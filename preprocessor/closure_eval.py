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

    closure_eval.py report [--days N] [--json|--md] [--labels PATH] [--no-write]

`report` writes `<private dir>/evals/closure-eval-latest.md` and `.json`
(0600) and prints the one-line summary that the daily health report
carries (summary_lines(), from mail_privacy.report). Counts and ids only:
the label store holds no text, and neither does this report.
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


def window(records: list[dict], days: float | None, now: float) -> list[dict]:
    if not days:
        return list(records)
    since = now - float(days) * 86400
    return [r for r in records if _t(r) >= since]


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


def score_model(taps: list[dict], models: list[dict], disagree: Counter) -> dict:
    idx = _model_index(models)
    overall, by_type = Confusion(), defaultdict(Confusion)
    covered = 0
    calib: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in taps:
        f = t["features"]
        gold = _cl.closed(t["label"])
        at = _t(t)
        m = _latest(idx, str(t.get("thread_ref")), at, ("judgment",))
        state = m["label"] if m else (f.get("prior_state") if f.get("prior_by") == "model"
                                      else None)
        pred = _cl.closed(state) if state else False
        covered += bool(state)
        overall.add(pred, gold)
        by_type[f.get("interaction_type") or "?"].add(pred, gold)
        if state and pred != gold:
            disagree[("model", f.get("interaction_type") or "?", "model", state, t["label"])] += 1
        raw = _latest(idx, str(t.get("thread_ref")), at, ("judgment", "unsure"))
        conf = (raw or {}).get("features", {}).get("confidence")
        if isinstance(conf, (int, float)):
            b = calib[_bucket(float(conf))]
            b[0] += 1
            b[1] += int(_cl.closed(raw["label"]) == gold)
    return {**overall.out(), "covered": covered,
            "by_interaction_type": {k: v.out() for k, v in sorted(by_type.items())},
            "calibration": {k: {"n": n, "accuracy": _r(ok / n) if n else None}
                            for k, (n, ok) in sorted(calib.items())},
            "judgments": sum(1 for m in models if m.get("kind") == "judgment"),
            "unsure": sum(1 for m in models if m.get("kind") == "unsure"),
            "unparsed": sum(1 for m in models if m.get("kind") == "unparsed")}


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
             now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    recs = window(records, days, now)
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
    model = score_model(taps, models, disagree)
    ledger = score_ledger(led, said)
    top = [{"who": k[0], "interaction_type": k[1], "rule_id": k[2], "predicted": k[3],
            "gold": k[4], "count": n} for k, n in disagree.most_common(TOP)]
    return {"generated_at": _cl.iso(now), "window_days": days, "labels": len(recs),
            "by_source": dict(by_source), "gold": gold, "owner_taps": len(taps),
            "rules": rules, "model": model, "ledger": ledger, "disagreements": top,
            "candidates": backtest(chat, taps)}


# ── output ────────────────────────────────────────────────────────────

def _f(x: Any) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"


def _window(days: Any) -> str:
    return f"last {days:g} days" if days else "all time"


def summary_line(rep: dict) -> str:
    r, m, led = rep["rules"], rep["model"], rep["ledger"]
    return (f"Closure labels: {rep['labels']} ({_window(rep['window_days'])}), "
            f"{rep['gold']} gold ({rep['owner_taps']} owner taps). "
            f"F1: rules {_f(r['f1'])} (n={r['n']}), model {_f(m['f1'])} (n={m['n']}), "
            f"ledger {_f(led['f1'])} (n={led['n']}).")


def _table(rows: list[tuple], head: tuple) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _prf_row(name: str, s: dict) -> tuple:
    return (name, s["n"], _f(s["precision"]), _f(s["recall"]), _f(s["f1"]))


def render_md(rep: dict) -> str:
    r, m, led = rep["rules"], rep["model"], rep["ledger"]
    src = ", ".join(f"{k} {v}" for k, v in sorted(rep["by_source"].items()))
    lines = ["# Closure detectors: evaluation", "",
             f"Generated {rep['generated_at']}. Window: {_window(rep['window_days'])}. "
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


def write_report(*, labels: str | Path | None = None, days: float | None = WINDOW_DAYS,
                 out_dir: str | Path | None = None, now: float | None = None) -> dict:
    """Evaluate and write the markdown and JSON reports beside the labels."""
    rep = evaluate(_cl.load(labels), days=days, now=now)
    d = Path(out_dir) if out_dir else Path(labels or _cl.labels_path()).parent
    _write_private(d / REPORT_MD, render_md(rep))
    _write_private(d / REPORT_JSON, json.dumps(rep, sort_keys=True, indent=1) + "\n")
    return rep


def summary_lines(labels: str | Path | None = None, days: float | None = WINDOW_DAYS) -> list[str]:
    """The daily health report's line; [] while the store holds nothing."""
    recs = _cl.load(labels)
    if not recs:
        return []
    return [summary_line(evaluate(recs, days=days))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="score the detectors against the gold labels",
                       allow_abbrev=False)
    r.add_argument("--days", type=float, default=WINDOW_DAYS,
                   help=f"the window in days (default {WINDOW_DAYS}; 0 for all)")
    r.add_argument("--labels", help="the label store (default: closure_labels.py's)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="print the whole report as JSON")
    g.add_argument("--md", action="store_true", help="print the markdown report")
    r.add_argument("--no-write", action="store_true", help="write no report file")
    a = ap.parse_args(argv)
    days = a.days or None
    rep = (evaluate(_cl.load(a.labels), days=days) if a.no_write
           else write_report(labels=a.labels, days=days))
    if a.json:
        print(json.dumps(rep, sort_keys=True, indent=1))
    elif a.md:
        print(render_md(rep))
    else:
        print(summary_line(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
