#!/usr/bin/env python3
"""Closure eval: the closure detectors scored against the owner's own labels.

Reads the label store (closure_labels.py; docs/ea/closure-signals.md) and
scores three detectors against its gold labels. The positive class is
"closed" (closure_labels.closed: done or none).

  rules   thread_state's rules at each owner tap: the state decide() gave
          for the thread then (`rule_state`). A tap the rules left to the
          model counts as "not closed" (an abstention); `covered` says how
          many taps a rule settled.
  model   thread_state's model: its latest answer on the thread before
          each owner tap (a `model` record of kind `judgment`), else the
          state the model had set (`prior_by` model). Calibration: every
          answer with a confidence (kind `judgment` or `unsure`) before a
          tap, by confidence bucket, against how often it was right.
          By model (`model_via`: local, hosted, or unknown for a record
          from before the field): each model's latest judgment before a
          tap, scored on the taps it judged, with its own calibration and
          answer counts; and how often the two agree where both judged
          one thread within a day, with the pairs that differ (refs and
          states only).
  ledger  the ledger closure pipeline. Caught: a row closed by a rule or
          the model, or a proposal the owner confirmed. Missed: a row the
          owner had to close in their own words. Wrong: a close the owner
          reopened, a proposal the owner rejected. By ledger type and by
          closing signal.

Gold: the owner's taps, the closures the owner stated, and the owner's
reopens, rejections and dispositions. Also: the top disagreement
patterns (slice, rule id or model, predicted, gold) and a backtest of
candidate rules over the recorded features: a card's status line, a
stuck turn, a pointer to a settled card, a linked ledger row completed,
and 48 hours of silence (the negative example; the closure analysis
found it wrong for 38% of the threads that end on the bot's prose).

    closure_eval.py report [--days N | --since DATE] [--json|--md] [--labels PATH] [--no-write]
    closure_eval.py trend [--last N] [--json] [--history PATH]

`report` writes `<private dir>/evals/closure-eval-latest.md` and `.json`
(0600) and prints the one-line summary that the daily health report
carries (summary_lines(), from mail_privacy.report). `--since` starts the
window at an ISO date (UTC) instead of N days back. The nightly
`closure_labels.py collect --report` also appends the report's numbers as
one JSON line to `<private dir>/evals/closure-eval-history.jsonl`; `trend`
prints the last N of them. Counts and ids only: the label store holds no
text, and neither does this report or its history.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

try:
    import closure_labels as _cl
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import closure_labels as _cl

WINDOW_DAYS = 30
REPORT_MD = "closure-eval-latest.md"
REPORT_JSON = "closure-eval-latest.json"
BUCKETS = ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0))
TOP = 8
CHAT_SOURCES = ("owner-tap", "model", "card")
# Which model gave an answer (the record's `model_via`); a record from
# before the field is `unknown`.
VIAS = ("local", "hosted", "unknown")
# Two answers on one thread at most this far apart count as one pair.
AGREE_WINDOW = 86400.0
DISAGREE_CAP = 20
HISTORY_FILE = "closure-eval-history.jsonl"
TREND_LAST = 14
# Ledger record kinds the owner's own act produced.
OWNER_LEDGER_KINDS = frozenset({"reopened", "rejected", "confirmed", "still_open", "done",
                                "not_mine", "snooze"})
CAUGHT = frozenset({"rule", "model", "confirmed"})

# (name, the class it predicts, when it fires)
CANDIDATES: tuple[tuple[str, str, Callable[[dict], bool]], ...] = (
    ("card_status_line", "done", lambda f: f.get("card_outcome") in _cl.SETTLED),
    ("stuck_turn", "open", lambda f: bool(f.get("stuck_turn"))),
    ("pointer_card_settled", "done",
     lambda f: bool(f.get("has_card_pointer")) and f.get("pointer_card_outcome") in _cl.SETTLED),
    ("ledger_row_completed", "done", lambda f: f.get("linked_ledger_status") == "completed"),
    ("silence_48h", "done", lambda f: f.get("last_author") == "bot"
     and (f.get("hours_since_last_message") or 0) >= 48),
)


class Confusion:
    """Counts for the positive class `closed`."""

    def __init__(self) -> None:
        self.tp = self.fp = self.fn = self.tn = 0

    def add(self, pred: bool, gold: bool) -> None:
        if pred and gold:
            self.tp += 1
        elif pred:
            self.fp += 1
        elif gold:
            self.fn += 1
        else:
            self.tn += 1

    def out(self) -> dict:
        return prf(self.tp, self.fp, self.fn, self.tn)


def prf(tp: int, fp: int, fn: int, tn: int = 0) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    # The harmonic mean of p and r, and 0 for a detector that never says
    # closed when the gold does.
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None
    n = tp + fp + fn + tn
    acc = (tp + tn) / n if n else None
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _r(p), "recall": _r(r), "f1": _r(f1), "accuracy": _r(acc)}


def _r(x: float | None) -> float | None:
    return round(x, 3) if x is not None else None


def _t(r: dict) -> float:
    return _cl.epoch(r.get("at")) or 0.0


def window(records: list[dict], days: float | None, now: float,
           since: float | None = None) -> list[dict]:
    """The records from `since` (an epoch), else from `days` back; all
    of them when neither is set."""
    if since is None:
        if not days:
            return list(records)
        since = now - float(days) * 86400
    return [r for r in records if _t(r) >= since]


def via_of(rec: dict) -> str:
    v = (rec.get("features") or {}).get("model_via")
    return v if v in ("local", "hosted") else "unknown"


def _bucket(conf: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= conf < hi or (hi == 1.0 and conf == 1.0):
            return f"{lo:.1f}-{hi:.1f}"
    return "?"


# ── the three detectors ───────────────────────────────────────────────

def score_rules(taps: list[dict], disagree: Counter) -> dict:
    overall, by_type, by_rule = Confusion(), defaultdict(Confusion), defaultdict(Confusion)
    covered = 0
    for t in taps:
        f = t["features"]
        gold = _cl.closed(t["label"])
        state = f.get("rule_state")
        pred = _cl.closed(state) if state else False
        covered += bool(state)
        overall.add(pred, gold)
        by_type[f.get("interaction_type") or "?"].add(pred, gold)
        by_rule[f.get("rule_id") or "model's"].add(pred, gold)
        if state and pred != gold:
            disagree[("rules", f.get("interaction_type") or "?", f.get("rule_id") or "?",
                      state, t["label"])] += 1
    return {**overall.out(), "covered": covered,
            "by_interaction_type": {k: v.out() for k, v in sorted(by_type.items())},
            "by_rule_id": {k: v.out() for k, v in sorted(by_rule.items())}}


def _model_index(models: list[dict]) -> dict[str, list[tuple[float, dict]]]:
    idx: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for m in models:
        idx[str(m.get("thread_ref"))].append((_t(m), m))
    for v in idx.values():
        v.sort(key=lambda x: x[0])
    return idx


def _latest(idx: dict, tref: str, at: float, kinds: tuple[str, ...]) -> dict | None:
    best = None
    for t, m in idx.get(tref, []):
        if t > at:
            break
        if m.get("kind") in kinds:
            best = m
    return best


def _calibration(taps: list[dict], idx: dict) -> dict:
    """The latest answer with a confidence before each tap, by confidence
    bucket, against how often its closed or open matched the tap."""
    calib: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in taps:
        raw = _latest(idx, str(t.get("thread_ref")), _t(t), ("judgment", "unsure"))
        conf = (raw or {}).get("features", {}).get("confidence")
        if isinstance(conf, (int, float)):
            b = calib[_bucket(float(conf))]
            b[0] += 1
            b[1] += int(_cl.closed(raw["label"]) == _cl.closed(t["label"]))
    return {k: {"n": n, "accuracy": _r(ok / n) if n else None}
            for k, (n, ok) in sorted(calib.items())}


def _answer_counts(answers: list[dict]) -> dict:
    kinds = Counter(m.get("kind") for m in answers)
    return {"judgments": kinds["judgment"], "unsure": kinds["unsure"],
            "unparsed": kinds["unparsed"]}


def score_via(taps: list[dict], models: list[dict], answers: list[dict]) -> dict:
    """Each model on its own: its latest judgment on the thread before
    each tap, scored on the taps it judged (`n`), with its calibration
    and its answers in the window."""
    out = {}
    for via in VIAS:
        idx = _model_index([m for m in models if via_of(m) == via])
        conf = Confusion()
        for t in taps:
            m = _latest(idx, str(t.get("thread_ref")), _t(t), ("judgment",))
            if m:
                conf.add(_cl.closed(m["label"]), _cl.closed(t["label"]))
        out[via] = {**conf.out(), "calibration": _calibration(taps, idx),
                    **_answer_counts([m for m in answers if via_of(m) == via])}
    return out


def agreement(answers: list[dict], taps: list[dict]) -> dict:
    """The local and the hosted model where both judged one thread within
    AGREE_WINDOW: each local judgment paired with the nearest hosted one
    not yet paired. Gold is the first owner tap on the thread after both."""
    sides: dict[str, dict[str, list[tuple[float, dict]]]] = defaultdict(
        lambda: {"local": [], "hosted": []})
    for m in answers:
        via = via_of(m)
        if m.get("kind") == "judgment" and via != "unknown":
            sides[str(m.get("thread_ref"))][via].append((_t(m), m))
    tap_idx: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for t in sorted(taps, key=_t):
        tap_idx[str(t.get("thread_ref"))].append((_t(t), t["label"]))
    pairs = []
    for tref, side in sides.items():
        loc, hos = side["local"], side["hosted"]
        cand = sorted((abs(lt - ht), i, j) for i, (lt, _) in enumerate(loc)
                      for j, (ht, _) in enumerate(hos) if abs(lt - ht) <= AGREE_WINDOW)
        used_l: set[int] = set()
        used_h: set[int] = set()
        for _dt, i, j in cand:
            if i in used_l or j in used_h:
                continue
            used_l.add(i)
            used_h.add(j)
            after = max(loc[i][0], hos[j][0])
            gold = next((lab for tt, lab in tap_idx.get(tref, []) if tt >= after), None)
            pairs.append((after, tref, loc[i][1]["label"], hos[j][1]["label"], gold))
    same_state = sum(1 for p in pairs if p[2] == p[3])
    same_closed = sum(1 for p in pairs if _cl.closed(p[2]) == _cl.closed(p[3]))
    differ = sorted((p for p in pairs if p[2] != p[3]), key=lambda p: (-p[0], p[1]))
    n = len(pairs)
    return {"pairs": n, "same_state": same_state, "same_closed": same_closed,
            "state_agreement": _r(same_state / n) if n else None,
            "closed_agreement": _r(same_closed / n) if n else None,
            "disagreement_count": len(differ),
            "disagreements": [{"thread_ref": p[1], "local": p[2], "hosted": p[3], "gold": p[4]}
                              for p in differ[:DISAGREE_CAP]]}


def score_model(taps: list[dict], models: list[dict], disagree: Counter,
                answers: list[dict] | None = None) -> dict:
    """`models`: every model record (an answer before a tap may be older
    than the window); `answers`: the ones in the window, which the counts
    and the agreement read."""
    answers = models if answers is None else answers
    idx = _model_index(models)
    overall, by_type = Confusion(), defaultdict(Confusion)
    covered = 0
    for t in taps:
        f = t["features"]
        gold = _cl.closed(t["label"])
        m = _latest(idx, str(t.get("thread_ref")), _t(t), ("judgment",))
        state = m["label"] if m else (f.get("prior_state") if f.get("prior_by") == "model"
                                      else None)
        pred = _cl.closed(state) if state else False
        covered += bool(state)
        overall.add(pred, gold)
        by_type[f.get("interaction_type") or "?"].add(pred, gold)
        if state and pred != gold:
            disagree[("model", f.get("interaction_type") or "?", "model", state, t["label"])] += 1
    return {**overall.out(), "covered": covered,
            "by_interaction_type": {k: v.out() for k, v in sorted(by_type.items())},
            "calibration": _calibration(taps, idx), **_answer_counts(answers),
            "by_via": score_via(taps, models, answers),
            "agreement": agreement(answers, taps)}


def score_ledger(led: list[dict], said: list[dict]) -> dict:
    """The pipeline as a closed/open detector over main-EA rows."""
    led = [r for r in led if r.get("origin") != "plugin"]

    def slot() -> dict:
        return {"caught": 0, "missed": 0, "other": 0, "wrong": 0, "confirmed": 0,
                "rejected": 0, "closes": 0, "reopened": 0}
    total, by_type, by_signal = slot(), defaultdict(slot), defaultdict(slot)
    how_counts: Counter = Counter()
    for r in led:
        f = r["features"]
        how = f.get("closed_how") or "other"
        typ = f.get("linked_ledger_type") or "?"
        sig = f.get("signal") or "none"
        kind = r["kind"]
        rows = (total, by_type[typ], by_signal[sig])
        if kind == "completed":
            how_counts[how] += 1
            key = "caught" if how in CAUGHT else ("missed" if how == "owner" else "other")
            for s in rows:
                s[key] += 1
                if how in ("rule", "model"):
                    s["closes"] += 1
        elif kind == "reopened" and how in CAUGHT:
            for s in rows:
                s["wrong"] += 1
                s["reopened"] += 1
        elif kind == "rejected":
            for s in rows:
                s["wrong"] += 1
                s["rejected"] += 1
        elif kind == "confirmed":
            for s in rows:
                s["confirmed"] += 1

    def metrics(s: dict) -> dict:
        tp = max(0, s["caught"] - s["reopened"])
        decided = s["confirmed"] + s["rejected"]
        return {**prf(tp, s["wrong"], s["missed"]), **s,
                "close_precision": _r(tp / s["caught"]) if s["caught"] else None,
                "proposal_precision": _r(s["confirmed"] / decided) if decided else None}
    no_fire = sum(1 for r in said if not r["features"].get("signals_fired"))
    fired = Counter(x for r in said for x in r["features"].get("signals_fired") or [])
    return {**metrics(total), "closed_how": dict(how_counts),
            "by_ledger_type": {k: metrics(v) for k, v in sorted(by_type.items())},
            "by_signal": {k: metrics(v) for k, v in sorted(
                by_signal.items(), key=lambda kv: -(kv[1]["caught"] + kv[1]["confirmed"]
                                                    + kv[1]["rejected"]))[:20]},
            "owner_said": {"n": len(said), "no_signal_fired": no_fire,
                           "share_no_signal": _r(no_fire / len(said)) if said else None,
                           "signals_fired": dict(fired.most_common(10)),
                           "suggestion": dict(Counter(r["features"].get("suggestion")
                                                      for r in said).most_common()),
                           "said_via": dict(Counter(r["features"].get("said_via")
                                                    for r in said).most_common())}}


def backtest(chat: list[dict], taps: list[dict]) -> list[dict]:
    """What each candidate rule would catch: where it fires over every chat
    record, and how often it agrees with the owner's taps."""
    out = []
    for name, predicts, fires in CANDIDATES:
        all_fires = sum(1 for r in chat if fires(r["features"]))
        gold_fires = agree = 0
        for t in taps:
            if fires(t["features"]):
                gold_fires += 1
                agree += int(_cl.closed(predicts) == _cl.closed(t["label"]))
        same_class = sum(1 for t in taps if _cl.closed(t["label"]) == _cl.closed(predicts))
        model_recs = [r for r in chat if r["source"] == "model" and r["kind"] == "judgment"
                      and fires(r["features"])]
        model_agree = sum(1 for r in model_recs if _cl.closed(r["label"]) == _cl.closed(predicts))
        out.append({"rule": name, "predicts": predicts, "fires": all_fires,
                    "fires_on_gold": gold_fires,
                    "precision": _r(agree / gold_fires) if gold_fires else None,
                    "recall": _r(agree / same_class) if same_class else None,
                    "model_agreement": _r(model_agree / len(model_recs)) if model_recs else None,
                    "model_n": len(model_recs)})
    return out


def evaluate(records: list[dict], *, days: float | None = WINDOW_DAYS,
             since: Any = None, now: float | None = None) -> dict:
    """The report. The window is `since` (an ISO date or an epoch) when
    set, else the last `days` days, else all time."""
    now = time.time() if now is None else float(now)
    since_t = _cl.epoch(since) if since is not None else None
    if since is not None and since_t is None:
        raise ValueError(f"not an ISO date: {since!r}")
    recs = window(records, days, now, since_t)
    by_source = Counter(r["source"] for r in recs)
    taps = sorted((r for r in recs if r["source"] == "owner-tap"), key=_t)
    # The model's answers before a tap may be older than the window.
    models = [r for r in records if r["source"] == "model"]
    led = [r for r in recs if r["source"] == "ledger"]
    said = [r for r in recs if r["source"] == "owner-said"]
    chat = [r for r in recs if r["source"] in CHAT_SOURCES]
    gold = (len(taps) + len(said)
            + sum(1 for r in led if r["kind"] in OWNER_LEDGER_KINDS
                  or r["features"].get("closed_how") == "owner"))
    disagree: Counter = Counter()
    rules = score_rules(taps, disagree)
    model = score_model(taps, models, disagree,
                        answers=[r for r in recs if r["source"] == "model"])
    ledger = score_ledger(led, said)
    top = [{"who": k[0], "interaction_type": k[1], "rule_id": k[2], "predicted": k[3],
            "gold": k[4], "count": n} for k, n in disagree.most_common(TOP)]
    return {"generated_at": _cl.iso(now), "window_days": None if since_t is not None else days,
            "since": _cl.iso(since_t) if since_t is not None else None, "labels": len(recs),
            "by_source": dict(by_source), "gold": gold, "owner_taps": len(taps),
            "rules": rules, "model": model, "ledger": ledger, "disagreements": top,
            "candidates": backtest(chat, taps)}


# ── output ────────────────────────────────────────────────────────────

def _f(x: Any) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"


def _window(rep: dict) -> str:
    since = rep.get("since")
    if since:
        return f"since {since[:10] if since.endswith('T00:00:00Z') else since}"
    days = rep.get("window_days")
    return f"last {days:g} days" if days else "all time"


def _by_model(m: dict) -> str:
    """'local F1 0.40 (n=12), hosted F1 0.71 (n=7), agree 0.80 (n=5)'; the
    unknown bucket only while it scores a tap."""
    bv = m.get("by_via") or {}
    parts = [f"{v} F1 {_f(bv[v]['f1'])} (n={bv[v]['n']})" for v in VIAS
             if v in bv and (v != "unknown" or bv[v]["n"])]
    ag = m.get("agreement") or {}
    parts.append(f"agree {_f(ag.get('state_agreement'))} (n={ag.get('pairs', 0)})")
    return ", ".join(parts)


def summary_line(rep: dict) -> str:
    r, m, led = rep["rules"], rep["model"], rep["ledger"]
    return (f"Closure labels: {rep['labels']} ({_window(rep)}), "
            f"{rep['gold']} gold ({rep['owner_taps']} owner taps). "
            f"F1: rules {_f(r['f1'])} (n={r['n']}), model {_f(m['f1'])} (n={m['n']}), "
            f"ledger {_f(led['f1'])} (n={led['n']}). Model {_by_model(m)}.")


def _table(rows: list[tuple], head: tuple) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _prf_row(name: str, s: dict) -> tuple:
    return (name, s["n"], _f(s["precision"]), _f(s["recall"]), _f(s["f1"]))


def _md_by_model(m: dict) -> list[str]:
    bv, ag = m["by_via"], m["agreement"]
    lines = ["", "### By model", "",
             "Each model against the taps it judged: its latest judgment on the thread "
             "before the tap. `unknown` holds the answers recorded before the record "
             "named the model.", ""]
    lines += _table([(v, s["n"], _f(s["precision"]), _f(s["recall"]), _f(s["f1"]),
                      s["judgments"], s["unsure"], s["unparsed"]) for v, s in bv.items()],
                    ("model", "taps judged", "precision", "recall", "F1", "judgments",
                     "unsure", "unparsed"))
    buckets = sorted({b for s in bv.values() for b in s["calibration"]})
    if buckets:
        lines += ["", "Calibration by model (accuracy, n):", ""]
        lines += _table([(b, *(f"{_f(c['accuracy'])} ({c['n']})"
                               if (c := bv[v]["calibration"].get(b)) else "-" for v in bv))
                         for b in buckets], ("confidence", *bv))
    lines += ["", f"Both models judged one thread within a day: {ag['pairs']} pairs. "
                  f"The same state: {ag['same_state']} ({_f(ag['state_agreement'])}). "
                  f"The same closed or open: {ag['same_closed']} "
                  f"({_f(ag['closed_agreement'])}).", "", "### Disagreements", ""]
    if ag["disagreements"]:
        lines += _table([(d["thread_ref"], d["local"], d["hosted"], d["gold"] or "-")
                         for d in ag["disagreements"]],
                        ("thread ref", "local", "hosted", "gold"))
        if ag["disagreement_count"] > len(ag["disagreements"]):
            lines += ["", f"The newest {len(ag['disagreements'])} of "
                          f"{ag['disagreement_count']}."]
    else:
        lines.append("None in the window.")
    return lines


def render_md(rep: dict) -> str:
    r, m, led = rep["rules"], rep["model"], rep["ledger"]
    src = ", ".join(f"{k} {v}" for k, v in sorted(rep["by_source"].items()))
    lines = ["# Closure detectors: evaluation", "",
             f"Generated {rep['generated_at']}. Window: {_window(rep)}. "
             f"Labels: {rep['labels']} ({src or 'none'}). Gold: {rep['gold']}. "
             f"Positive class: closed.", "",
             "## Thread-state rules (against the owner's taps)", "",
             f"Taps: {r['n']}. A rule settled {r['covered']}; the rest went to the model.", ""]
    lines += _table([_prf_row("all", r)] + [_prf_row(k, v) for k, v in
                                            r["by_interaction_type"].items()],
                    ("interaction type", "n", "precision", "recall", "F1"))
    lines += [""] + _table([_prf_row(k, v) for k, v in r["by_rule_id"].items()],
                           ("rule id", "n", "precision", "recall", "F1"))
    lines += ["", "## Thread-state model (against the owner's taps)", "",
              f"Taps with a model state: {m['covered']} of {m['n']}. Answers recorded: "
              f"{m['judgments']} judgments, {m['unsure']} unsure, {m['unparsed']} unparsed.", ""]
    lines += _table([_prf_row("all", m)] + [_prf_row(k, v) for k, v in
                                            m["by_interaction_type"].items()],
                    ("interaction type", "n", "precision", "recall", "F1"))
    if m["calibration"]:
        lines += ["", "Calibration (the model's confidence against how often it was right):", ""]
        lines += _table([(k, v["n"], _f(v["accuracy"])) for k, v in m["calibration"].items()],
                        ("confidence", "n", "accuracy"))
    lines += _md_by_model(m)
    lines += ["", "## Ledger closure pipeline", "",
              f"Caught {led['caught']} (rule or model close, or a confirmed proposal), missed "
              f"{led['missed']} (the owner's word), other {led['other']}. Wrong: "
              f"{led['reopened']} reopened, {led['rejected']} rejected proposals.", ""]
    lines += _table([_prf_row("all", led)] + [_prf_row(k, v) for k, v in
                                              led["by_ledger_type"].items()],
                    ("ledger type", "n", "precision", "recall", "F1"))
    lines += [""] + _table([(k, v["caught"], v["reopened"], _f(v["close_precision"]),
                             v["confirmed"], v["rejected"], _f(v["proposal_precision"]))
                            for k, v in led["by_signal"].items()],
                           ("signal", "closes", "reopened", "close precision",
                            "proposals confirmed", "rejected", "proposal precision"))
    os_ = led["owner_said"]
    lines += ["", f"Closures the owner had to state: {os_['n']}; no signal fired for "
                  f"{os_['no_signal_fired']} ({_f(os_['share_no_signal'])})."]
    lines += ["", "## Top disagreements", ""]
    if rep["disagreements"]:
        lines += _table([(d["who"], d["interaction_type"], d["rule_id"], d["predicted"],
                          d["gold"], d["count"]) for d in rep["disagreements"]],
                        ("who", "interaction type", "rule id", "predicted", "gold", "count"))
    else:
        lines.append("None in the window.")
    lines += ["", "## Candidate rules (backtest over the features)", ""]
    lines += _table([(c["rule"], c["predicts"], c["fires"], c["fires_on_gold"],
                      _f(c["precision"]), _f(c["recall"]), _f(c["model_agreement"]))
                     for c in rep["candidates"]],
                    ("rule", "predicts", "fires", "on taps", "precision", "recall",
                     "agrees with model"))
    lines += ["", summary_line(rep), ""]
    return "\n".join(lines)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def history_path() -> Path:
    return _cl.labels_path().parent / HISTORY_FILE


def history_entry(rep: dict) -> dict:
    """The report's numbers for the history: counts and scores only."""
    def prf_(s: dict) -> dict:
        return {k: s[k] for k in ("n", "precision", "recall", "f1")}
    m = rep["model"]
    return {"at": rep["generated_at"], "window_days": rep["window_days"],
            "since": rep.get("since"), "labels": rep["labels"], "gold": rep["gold"],
            "owner_taps": rep["owner_taps"], "rules": prf_(rep["rules"]), "model": prf_(m),
            "ledger": prf_(rep["ledger"]),
            "by_via": {v: {**prf_(s), "judgments": s["judgments"]}
                       for v, s in m["by_via"].items()},
            "agreement": {k: m["agreement"][k]
                          for k in ("pairs", "state_agreement", "closed_agreement")}}


def append_history(rep: dict, path: str | Path | None = None) -> Path:
    """Append the report's numbers as one JSON line (0600, in a 0700 dir;
    a link at the name is refused)."""
    path = Path(path or history_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(history_entry(rep), sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        if os.fstat(fh.fileno()).st_mode & 0o077:
            os.fchmod(fh.fileno(), 0o600)
        fh.write(line)
    return path


def load_history(path: str | Path | None = None) -> list[dict]:
    """The history's entries, oldest first; a line that does not parse is
    skipped."""
    try:
        text = Path(path or history_path()).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("at"):
            out.append(e)
    return out


def _cell(s: dict | None) -> str:
    return f"{_f((s or {}).get('f1'))} ({(s or {}).get('n', 0)})"


TREND_HEAD = ("run", "labels", "gold", "rules", "model", "local", "hosted", "unknown",
              "ledger", "agree")


def trend_rows(entries: list[dict]) -> list[tuple]:
    rows = []
    for e in entries:
        bv, ag = e.get("by_via") or {}, e.get("agreement") or {}
        rows.append((str(e["at"])[:16].replace("T", " "), e.get("labels", 0), e.get("gold", 0),
                     _cell(e.get("rules")), _cell(e.get("model")), _cell(bv.get("local")),
                     _cell(bv.get("hosted")), _cell(bv.get("unknown")), _cell(e.get("ledger")),
                     f"{_f(ag.get('state_agreement'))} ({ag.get('pairs', 0)})"))
    return rows


def render_trend(entries: list[dict]) -> str:
    """The entries as aligned columns: F1 (n) per detector and model, and
    the two models' agreement (pairs)."""
    rows = [TREND_HEAD] + trend_rows(entries)
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(TREND_HEAD))]
    return "\n".join("  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                               for i, (c, w) in enumerate(zip(r, widths))).rstrip()
                     for r in rows)


def write_report(*, labels: str | Path | None = None, days: float | None = WINDOW_DAYS,
                 since: Any = None, out_dir: str | Path | None = None,
                 now: float | None = None, history: bool = False) -> dict:
    """Evaluate and write the markdown and JSON reports beside the labels;
    with `history`, also append the numbers to the history there."""
    rep = evaluate(_cl.load(labels), days=days, since=since, now=now)
    d = Path(out_dir) if out_dir else Path(labels or _cl.labels_path()).parent
    _write_private(d / REPORT_MD, render_md(rep))
    _write_private(d / REPORT_JSON, json.dumps(rep, sort_keys=True, indent=1) + "\n")
    if history:
        append_history(rep, d / HISTORY_FILE)
    return rep


def summary_lines(labels: str | Path | None = None, days: float | None = WINDOW_DAYS,
                  since: Any = None) -> list[str]:
    """The daily health report's line; [] while the store holds nothing."""
    recs = _cl.load(labels)
    if not recs:
        return []
    return [summary_line(evaluate(recs, days=days, since=since))]


def _since_arg(value: str) -> str:
    if _cl.epoch(value) is None:
        raise argparse.ArgumentTypeError(f"not an ISO date: {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="score the detectors against the gold labels",
                       allow_abbrev=False)
    w = r.add_mutually_exclusive_group()
    w.add_argument("--days", type=float, default=WINDOW_DAYS,
                   help=f"the window in days (default {WINDOW_DAYS}; 0 for all)")
    w.add_argument("--since", type=_since_arg,
                   help="the window from this ISO date (UTC) instead of --days")
    r.add_argument("--labels", help="the label store (default: closure_labels.py's)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="print the whole report as JSON")
    g.add_argument("--md", action="store_true", help="print the markdown report")
    r.add_argument("--no-write", action="store_true", help="write no report file")
    t = sub.add_parser("trend", help="the last nightly reports, one row each",
                       allow_abbrev=False)
    t.add_argument("--last", type=int, default=TREND_LAST,
                   help=f"how many runs (default {TREND_LAST})")
    t.add_argument("--history", help=f"the history (default <private dir>/evals/{HISTORY_FILE})")
    t.add_argument("--json", action="store_true", help="print the entries as JSON lines")
    a = ap.parse_args(argv)
    if a.cmd == "trend":
        entries = load_history(a.history)[-a.last:] if a.last > 0 else []
        if a.json:
            for e in entries:
                print(json.dumps(e, sort_keys=True))
        elif entries:
            print(render_trend(entries))
        else:
            print("No closure eval history yet: the nightly "
                  "`closure_labels.py collect --report` writes it.")
        return 0
    days = a.days or None
    rep = (evaluate(_cl.load(a.labels), days=days, since=a.since) if a.no_write
           else write_report(labels=a.labels, days=days, since=a.since))
    if a.json:
        print(json.dumps(rep, sort_keys=True, indent=1))
    elif a.md:
        print(render_md(rep))
    else:
        print(summary_line(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
