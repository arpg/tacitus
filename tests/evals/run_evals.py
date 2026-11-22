#!/usr/bin/env python3
"""
Standalone eval runner for EA system integration tests.

Runs the eval suite without requiring promptfoo (npm). Uses the same
provider, fixtures, and judge functions. For the full promptfoo experience
(LLM-as-judge rubrics, CI/CD thresholds, web UI), use:
  cd tests/evals && npx promptfoo eval

This standalone runner supports:
  - prompt-only mode (fast, no tokens): validates prompt construction
  - full mode (slow, uses tokens): invokes Claude and checks output
  - Custom Python assertions from judge.py
  - Category filtering (--category regression, --category hard-success)
  - JSON output for CI integration

Usage:
  python3 tests/evals/run_evals.py                      # prompt-only, all
  python3 tests/evals/run_evals.py --mode full           # invoke Claude
  python3 tests/evals/run_evals.py --category regression # regressions only
  python3 tests/evals/run_evals.py --repeat 3            # 3x for variance
  python3 tests/evals/run_evals.py --json                # machine-readable
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))

from provider import call_api
from judge import ASSERTIONS


def load_fixtures(category: str | None = None,
                  only: set[str] | None = None) -> list[dict]:
    """Load all test fixtures, optionally filtered by category and/or id.

    `only` matches either the `_meta.id` (e.g. "regression-002") or the file
    stem (e.g. "already-replied-thread"), so a subset run can be requested by
    whichever name the caller has to hand. A subset run validates a harness
    fix on 2-3 fixtures instead of paying for the whole suite.
    """
    fixtures = []
    fixture_dir = EVAL_DIR / "fixtures"
    for f in sorted(fixture_dir.glob("*.json")):
        with open(f) as fh:
            data = json.load(fh)
        meta = data.get("_meta", {})
        if category and meta.get("category") != category:
            continue
        if only and not ({meta.get("id", f.stem), f.stem} & only):
            continue
        fixtures.append({
            "path": str(f),
            "name": meta.get("name", f.stem),
            "id": meta.get("id", f.stem),
            "category": meta.get("category", "unknown"),
            "description": meta.get("description", ""),
            "bug_ref": meta.get("bug_ref", ""),
        })
    return fixtures


_ANCHOR_RE = re.compile(r"^\s*:[a-z0-9_]+:")


def _strip_preamble_like_production(output: str) -> tuple[str, int]:
    """Mirror run-agent.sh strip_preamble, in its bounded form.

    Production scans the first STRIP_PREAMBLE_MAX_LINES (default 12) lines
    for the first Slack-shortcode-anchored line and strips everything above
    it before validating and posting; deeper or absent anchors pass the
    output through verbatim. The judge must grade what Slack would see.
    Returns (possibly-stripped output, number of lines stripped).
    """
    bound = int(os.environ.get("STRIP_PREAMBLE_MAX_LINES", "12"))
    lines = output.splitlines()
    for i, line in enumerate(lines[:bound]):
        if _ANCHOR_RE.match(line):
            if i == 0:
                return output, 0
            return "\n".join(lines[i:]), i
    return output, 0


def run_eval(fixture_info: dict, mode: str, model: str,
             timeout: int, harness: str = "claude") -> dict:
    """Run a single eval and return results. `harness` is "claude" (the
    default, `claude -p`) or "codex" (`codex exec`, the same prompt with
    the operating instructions embedded); `model` names a model of that
    harness ("" keeps codex's local default)."""
    fixture_path = fixture_info["path"]

    # Invoke the provider
    result = call_api(
        prompt=f"Evaluate: {fixture_info['name']}",
        options={"config": {
            "mode": mode,
            "model": model,
            "skill": "morning-briefing",
            "timeout": timeout,
            "harness": harness,
        }},
        context={"vars": {"fixture_path": fixture_path}},
    )

    output = result.get("output", "")
    metadata = result.get("metadata", {})
    harness_error = metadata.get("harness_error", "")

    # The suite could not be MEASURED for this fixture (quota block, auth
    # failure, timeout, missing fixture). Judging an empty string here
    # returns verdicts about a model that was never reached: the
    # data-only assertions pass and the format check fails. Return an
    # ERROR verdict and let the caller route it to the harness-failure
    # path.
    if harness_error:
        return {
            "fixture": fixture_info,
            "pass": False,
            "error": harness_error,
            "score": 0.0,
            "assertions": [],
            "output_length": 0,
            "duration_s": metadata.get("duration_s", 0),
            "output_preview": "(harness error)",
        }

    # Run relevant assertions based on fixture category and content
    assertion_results = []
    context = {
        "vars": {"fixture_path": fixture_path},
        "providerResponse": {"metadata": metadata},
    }

    # Determine which assertions apply to this fixture
    with open(fixture_path) as _fh:
        _fixture_data = json.load(_fh)
    _meta = _fixture_data.get("_meta", {})
    _fixture_id = _meta.get("id", "")
    _briefing = _fixture_data.get("briefing", {})

    # Map fixture IDs to their specific assertions
    fixture_assertions = {
        "regression-001": ["no_overdue_completed"],
        "regression-002": ["already_replied_informational"],
        "regression-003": ["delta_unreplied_resurface"],
        "regression-004": ["normal_email_surfaces"],
        "regression-005": ["multi_step_decomposition"],
        "regression-006": ["name_from_header"],
        "regression-007": ["no_sandbox_leak"],
        "regression-008": ["reopened_thread_surfaced"],
        "regression-009": ["briefing_format_valid"],
        "hard-success-001": ["cc_only_no_reply_suggestion"],
        "hard-success-002": ["priority_ordering"],
        "hard-success-003": ["calm_day_concise"],
        "hard-success-004": ["single_step_no_split"],
        "hard-success-005": ["zoom_not_teams"],
        "hard-success-006": ["poll_reply_sender_only"],
        "hard-success-007": ["draft_revision_uses_update"],
        "hard-success-scheduling-defaults": ["scheduling_defaults"],
        "hard-success-no-dead-scheduling-tools": ["no_dead_scheduling_tools"],
        "hard-success-scheduling-canonical-rules": [
            "scheduling_rules_has_canonical_block",
            "scheduling_rules_lists_canonical_four",
        ],
        "hard-success-letter-one-decision": ["letter_request_one_decision"],
    }

    applicable = list(fixture_assertions.get(_fixture_id, ASSERTIONS.keys()))

    # In full mode, also run the format check. Every fixture in this suite is a
    # morning-briefing run (the scenario lives in briefing.recent_slack_messages
    # and provider.py always invokes skill=morning-briefing), so the briefing
    # format contract genuinely applies to all of them. Dedupe, because a
    # fixture may already list briefing_format_valid.
    if mode == "full" and "briefing_format_valid" not in applicable:
        applicable.append("briefing_format_valid")

    # Judge what production posts, not the raw final turn: run-agent.sh pipes
    # the output through strip_preamble before validating and posting, so a
    # short narration line above the header never reaches Slack, so it
    # must not fail a briefing production would post clean.
    # The strip is surfaced as an informational assertion so the narration
    # habit stays visible in reports without failing production-clean output.
    if mode == "full":
        output, _n_stripped = _strip_preamble_like_production(output)
        if _n_stripped:
            assertion_results.append({
                "assertion": "preamble_stripped",
                "pass": True,
                "score": 1.0,
                "reason": (f"{_n_stripped} narration line(s) above the header; "
                           f"production strip_preamble removes these before "
                           f"posting, but the model should not write them"),
            })

    # An empty final turn in full mode is a real, production-relevant failure
    # (run-agent.sh:339-342: text written before a tool call is dropped), but
    # it is invisible to the data-only assertions, which read the fixture
    # and not the output. Pin it explicitly so an empty briefing can never
    # be scored as a pass.
    if mode == "full" and not output.strip():
        assertion_results.append({
            "assertion": "non_empty_output",
            "pass": False,
            "score": 0.0,
            "reason": "Model returned no final assistant text",
        })

    for name in applicable:
        fn = ASSERTIONS.get(name)
        if not fn:
            continue
        try:
            r = fn(output, context)
            assertion_results.append({
                "assertion": name,
                "pass": r["pass"],
                "score": r["score"],
                "reason": r["reason"],
            })
        except Exception as e:
            assertion_results.append({
                "assertion": name,
                "pass": False,
                "score": 0.0,
                "reason": f"Error: {e}",
            })

    all_pass = all(r["pass"] for r in assertion_results)
    avg_score = (sum(r["score"] for r in assertion_results)
                 / len(assertion_results) if assertion_results else 0)

    # Opik trace (best-effort, suppress connection warnings)
    try:
        import logging as _logging
        _logging.getLogger("opik").setLevel(_logging.CRITICAL)
        sys.path.insert(0, str(EVAL_DIR.parent.parent / "preprocessor"))
        from opik_trace import trace_eval as _trace_eval
        _trace_eval(fixture_info, assertion_results, output, mode)
    except Exception:
        pass

    result = {
        "fixture": fixture_info,
        "pass": all_pass,
        "score": avg_score,
        "assertions": assertion_results,
        "output_length": len(output),
        "duration_s": metadata.get("duration_s", 0),
        "output_preview": output[:200] if output else "(empty)",
    }
    if output:
        # Every run keeps its whole output (capped). A 200-char preview
        # cannot show which phrase a judge matched, and a passing run's
        # text is what a judge fix is checked against.
        result["output_full"] = output[:8000]
    if metadata.get("stages_events"):
        # What the run staged, so a failing invite can be read in the log.
        result["staged_event"] = metadata.get("staged_event")
        result["production_state_writes"] = metadata.get("production_state_writes", [])
    return result


def main():
    parser = argparse.ArgumentParser(
        description="EA system integration eval runner")
    parser.add_argument("--mode", choices=["prompt-only", "full"],
                        default="prompt-only",
                        help="prompt-only (fast) or full (invoke Claude)")
    parser.add_argument("--model", default="sonnet",
                        help="Claude model for full mode (a Codex model name with --harness codex; "
                             "'default' keeps codex's own)")
    parser.add_argument("--harness", choices=["claude", "codex"], default="claude",
                        help="which agent harness runs the skill in full mode")
    # A full-mode fixture runs 78-180 s, so the default leaves headroom.
    # Production puts no timeout on the claude call at all
    # (run-agent.sh:470), so this is purely a harness stop-loss.
    parser.add_argument("--timeout", type=int, default=420,
                        help="Timeout per test in full mode (seconds)")
    parser.add_argument("--category", choices=["regression", "hard-success"],
                        help="Filter by test category")
    parser.add_argument("--fixture", action="append", default=[],
                        metavar="ID",
                        help="Run only these fixtures (repeatable, or comma-"
                             "separated). Accepts _meta.id or file stem.")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Run each test N times (for variance detection)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show assertion details")
    args = parser.parse_args()

    only = {name.strip() for arg in args.fixture
            for name in arg.split(",") if name.strip()} or None

    fixtures = load_fixtures(args.category, only)
    if not fixtures:
        print("No fixtures found.", file=sys.stderr)
        sys.exit(1)
    if only:
        found = {f["id"] for f in fixtures} | {
            Path(f["path"]).stem for f in fixtures}
        missing = sorted(only - found)
        if missing:
            print(f"Unknown fixture(s): {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

    if not args.json:
        cat_label = f" ({args.category})" if args.category else ""
        print(f"\nEA Integration Evals — {len(fixtures)} fixtures{cat_label}, "
              f"mode={args.mode}, repeat={args.repeat}")
        print("=" * 60)

    all_results = []
    total_pass = 0
    total_fail = 0
    total_error = 0
    total_hard_success = 0
    total_hard_success_pass = 0

    for fixture in fixtures:
        run_results = []
        for run_idx in range(args.repeat):
            start = time.time()
            model = "" if (args.harness == "codex" and args.model in ("sonnet", "default")) else args.model
            result = run_eval(fixture, args.mode, model, args.timeout, harness=args.harness)
            result["run_index"] = run_idx
            result["wall_time_s"] = time.time() - start
            run_results.append(result)

        # Aggregate across repeats
        errors = [r for r in run_results if r.get("error")]
        passes = sum(1 for r in run_results if r["pass"])
        pass_rate = passes / len(run_results)
        is_category = fixture["category"]

        # ERROR is not FAIL. A fixture whose claude invocation never
        # returned says nothing about the system under test, so it is not
        # a behavioral failure and is excluded from the hard-success
        # denominator.
        if errors:
            total_error += 1
            status = "ERROR"
        else:
            if is_category == "hard-success":
                total_hard_success += 1
                if pass_rate == 1.0:
                    total_hard_success_pass += 1

            if pass_rate == 1.0:
                total_pass += 1
                status = "PASS"
            elif pass_rate > 0:
                total_fail += 1
                status = f"FLAKY ({passes}/{len(run_results)})"
            else:
                total_fail += 1
                status = "FAIL"

        tag = ("\u2705" if status == "PASS"
               else "\U0001f6a8" if status == "ERROR"
               else "\u26a0\ufe0f" if "FLAKY" in status
               else "\u274c")

        if not args.json:
            print(f"\n  {tag} [{is_category}] {fixture['name']}")
            if fixture.get("bug_ref"):
                print(f"     Bug ref: {fixture['bug_ref']}")
            print(f"     Status: {status} | Avg score: "
                  f"{sum(r['score'] for r in run_results) / len(run_results):.2f}")

            if args.verbose or status != "PASS":
                for r in run_results:
                    if r.get("error"):
                        print(f"       \U0001f6a8 harness: {r['error']}")
                    for a in r["assertions"]:
                        a_tag = "\u2705" if a["pass"] else "\u274c"
                        print(f"       {a_tag} {a['assertion']}: {a['reason']}")

        all_results.append({
            "fixture": fixture,
            "runs": run_results,
            "pass_rate": pass_rate,
            "status": status,
            "error": errors[0]["error"] if errors else "",
        })

    if args.json:
        print(json.dumps({
            "total": len(fixtures),
            "passed": total_pass,
            "failed": total_fail,
            "errors": total_error,
            "hard_successes": f"{total_hard_success_pass}/{total_hard_success}",
            "results": all_results,
        }, indent=2, default=str))
    else:
        print("\n" + "=" * 60)
        print(f"  Total: {len(fixtures)} | "
              f"Pass: {total_pass} | Fail: {total_fail} | "
              f"Harness errors: {total_error}")
        if total_hard_success:
            print(f"  Hard successes preserved: "
                  f"{total_hard_success_pass}/{total_hard_success}")
        print("=" * 60)

    # Exit codes are three-valued so the nightly wrapper can tell "a fixture
    # regressed" (fix the system) from "the suite could not run" (fix the
    # harness / wait for quota). run_full_if_changed.sh routes 2 to its
    # EVAL HARNESS FAILURE path and un-claims the commit so the next night
    # retries it.
    if total_error:
        sys.exit(2)
    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
