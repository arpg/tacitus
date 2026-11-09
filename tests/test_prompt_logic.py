#!/usr/bin/env python3
"""
Regression tests for EA Slack bot prompt routing logic.

Runs test cases from test_cases.json against functions in
preprocessor/prompt_logic.py. Reports per-function scorecard
and overall pass rate.

Usage:
    python3 tests/test_prompt_logic.py
    python3 tests/test_prompt_logic.py --tag former-false-positive
    python3 tests/test_prompt_logic.py --function detect_needed_modules
    python3 tests/test_prompt_logic.py --verbose
    python3 tests/test_prompt_logic.py --json
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from preprocessor.prompt_logic import (
    detect_needed_modules,
    build_claude_md,
    extract_relevant_briefing,
    detect_skills,
    needs_mcp,
    topic_text,
)

CASES_FILE = Path(__file__).resolve().parent / "test_cases.json"
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"


# -------------------------------------------------------------------
# Result tracking
# -------------------------------------------------------------------

class Results:
    def __init__(self):
        self.by_function: dict[str, dict] = {}
        self.failures: list[dict] = []

    def record(self, func_name: str, case_id: str, passed: bool,
               expected=None, actual=None, message: str = "", verbose_info: str = ""):
        if func_name not in self.by_function:
            self.by_function[func_name] = {"passed": 0, "failed": 0, "total": 0}
        self.by_function[func_name]["total"] += 1
        if passed:
            self.by_function[func_name]["passed"] += 1
        else:
            self.by_function[func_name]["failed"] += 1
            self.failures.append({
                "case_id": case_id,
                "function": func_name,
                "expected": expected,
                "actual": actual,
                "message": message,
                "verbose_info": verbose_info,
            })

    def print_scorecard(self, verbose=False):
        total_passed = 0
        total_tests = 0

        print()
        print("=" * 60)
        print("EA Prompt Logic Tests — Scorecard")
        print("=" * 60)
        print()

        max_name = max(len(f) for f in self.by_function) if self.by_function else 20
        for func_name, stats in sorted(self.by_function.items()):
            p = stats["passed"]
            t = stats["total"]
            pct = (p / t * 100) if t > 0 else 0
            bar = "PASS" if p == t else "FAIL"
            print(f"  {func_name:<{max_name}}  {p:>3}/{t:<3} ({pct:5.1f}%)  [{bar}]")
            total_passed += p
            total_tests += t

        pct = (total_passed / total_tests * 100) if total_tests > 0 else 0
        print()
        print(f"  {'OVERALL':<{max_name}}  {total_passed:>3}/{total_tests:<3} ({pct:5.1f}%)")
        print()

        if self.failures:
            print("-" * 60)
            print("FAILURES:")
            print("-" * 60)
            for f in self.failures:
                print(f"\n  [{f['case_id']}] {f['function']}")
                print(f"    expected = {f['expected']!r}")
                print(f"    actual   = {f['actual']!r}")
                if f["message"]:
                    print(f"    note: {f['message']}")
                if verbose and f["verbose_info"]:
                    print(f"    msg: {f['verbose_info']!r}")
            print()

    def to_json(self) -> dict:
        total_passed = sum(s["passed"] for s in self.by_function.values())
        total_tests = sum(s["total"] for s in self.by_function.values())
        return {
            "total_passed": total_passed,
            "total_tests": total_tests,
            "pass_rate": round(total_passed / total_tests * 100, 1) if total_tests else 0,
            "by_function": self.by_function,
            "failures": self.failures,
        }


# -------------------------------------------------------------------
# Test runners
# -------------------------------------------------------------------

def run_case_tests(cases: list[dict], results: Results, functions: set[str] | None):
    """Run detect_needed_modules and context_routing for each test case."""

    for case in cases:
        cid = case["id"]
        msg = case["message"]
        expected = case["expected"]
        notes = case.get("notes", "")

        # --- detect_needed_modules ---
        if functions is None or "detect_needed_modules" in functions:
            exp_modules = set(expected.get("modules", []))
            actual_modules = detect_needed_modules(msg)
            ok = actual_modules == exp_modules
            results.record(
                "detect_needed_modules", cid, ok,
                expected=sorted(exp_modules),
                actual=sorted(actual_modules),
                message=notes,
                verbose_info=msg,
            )

        # --- context_routing (tool/workflow routing check) ---
        if functions is None or "context_routing" in functions:
            exp_context = expected.get("context")
            if exp_context is not None:
                actual_modules = detect_needed_modules(msg)
                _module_to_context = {
                    "scheduling": "needs_scheduling_engine",
                    "polls": "needs_poll_filler",
                    "reimbursement": "needs_reimbursement_workflow",
                    "lor": "needs_lor_workflow",
                    "snooze": "needs_snooze_workflow",
                }
                for module, context_key in _module_to_context.items():
                    exp_val = exp_context.get(context_key)
                    if exp_val is not None:
                        actual_val = module in actual_modules
                        ok = actual_val == exp_val
                        results.record(
                            "context_routing", cid, ok,
                            expected=f"{context_key}={exp_val}",
                            actual=f"{context_key}={actual_val} (modules={sorted(actual_modules)})",
                            message=notes,
                            verbose_info=msg,
                        )


def run_build_claude_md_tests(bm_cases: list[dict], results: Results, functions: set[str] | None):
    """Run build_claude_md structural tests."""
    if functions is not None and "build_claude_md" not in functions:
        return

    if not CLAUDE_MD.exists():
        print(f"  WARNING: {CLAUDE_MD} not found, skipping build_claude_md tests")
        return

    full_text = CLAUDE_MD.read_text()

    for case in bm_cases:
        cid = case["id"]
        needed = set(case["needed_modules"])
        assertions = case["assertions"]
        notes = case.get("notes", "")

        output = build_claude_md(needed, full_text)

        for s in assertions.get("must_contain", []):
            ok = s in output
            results.record(
                "build_claude_md", cid, ok,
                expected=f"contains {s!r}",
                actual="MISSING" if not ok else "present",
                message=notes,
            )

        for s in assertions.get("must_not_contain", []):
            ok = s not in output
            results.record(
                "build_claude_md", cid, ok,
                expected=f"NOT contains {s!r}",
                actual="PRESENT (should be stripped)" if not ok else "absent",
                message=notes,
            )

        for s in assertions.get("must_contain_replacements", []):
            ok = s in output
            results.record(
                "build_claude_md", cid, ok,
                expected=f"replacement {s!r}",
                actual="MISSING" if not ok else "present",
                message=notes,
            )


# -------------------------------------------------------------------
# Briefing extraction tests
# -------------------------------------------------------------------

_MOCK_BRIEFING = {
    "generated_at": "2026-04-03T21:00:00+00:00",
    "open_obligations": [
        {
            "id": "response-eli-poll",
            "type": "scheduling_request",
            "status": "open",
            "description": "Needs response: PhD Defense Scheduling Poll",
            "from_email": "eli.prescott@example.edu",
            "source_message_id": "AAMk-eli-123",
            "notes": "Hi Kit, please fill out https://timeful.app/e/Ex4mp for my defense in late May.",
            "last_action": "",
            "conversation_id": "conv-eli",
        },
        {
            "id": "response-program-review",
            "type": "response_needed",
            "status": "open",
            "description": "Needs response: Re: Program review / Ashcombe / Congrats",
            "from_email": "harper.ashcombe@example.edu",
            "source_message_id": "AAMk-harper-456",
            "notes": "Hi all, A couple of quick updates: I created the routing form! Routed for approval to the PIs.",
            "last_action": "",
            "conversation_id": "conv-harper",
        },
        {
            "id": "response-oakley-resched",
            "type": "response_needed",
            "status": "open",
            "description": "Needs response: Re: Rescheduling our Apr 14 weekly",
            "from_email": "oakley.stavros@example.edu",
            "source_message_id": "AAMk-oakley-789",
            "notes": "Let's do Monday! Thanks!",
            "last_action": "",
            "conversation_id": "conv-oakley",
        },
        {
            "id": "response-cam-resched",
            "type": "response_needed",
            "status": "open",
            "description": "Needs response: Re: Rescheduling our Apr 14 weekly",
            "from_email": "cam.prescott@example.edu",
            "source_message_id": "AAMk-cam-101",
            "notes": "Let's do Monday the 13th at 10:30.",
            "last_action": "",
            "conversation_id": "conv-cam",
        },
        {
            "id": "response-ash-doc",
            "type": "response_needed",
            "status": "open",
            "description": "Needs response: Short doc for Tech Access",
            "from_email": "ash.ravensworth@example.edu",
            "source_message_id": "AAMk-ash-202",
            "notes": "Ash Ravensworth (CS Dean) shared draft doc for state-grant review.",
            "last_action": "",
            "conversation_id": "conv-ash",
        },
    ],
    "stale_obligations": [],
    "availability_merged": [
        {"date": "2026-04-07", "start": "13:00", "end": "14:00", "duration_min": 60},
    ],
    "working_hours": {"daysOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday"],
                      "startTime": "08:30:00", "endTime": "16:30:00",
                      "timeZone": {"name": "Mountain Standard Time"}},
    "deadline_items": [],
}


def run_briefing_extract_tests(results: Results, functions: set[str] | None):
    """Test extract_relevant_briefing against mock briefing data."""
    if functions is not None and "extract_relevant_briefing" not in functions:
        return

    # Test 1: Eli poll: should find Eli entry with timeful URL
    out = extract_relevant_briefing("Fill out the Eli PhD Defense Poll on Timeful",
                                     _MOCK_BRIEFING)
    results.record("extract_relevant_briefing", "briefing-eli-poll",
                   "response-eli-poll" in out and "timeful.app" in out,
                   expected="eli entry with timeful URL",
                   actual=out[:200] if out else "(empty)")

    # Test 2: Oakley 1:1: should find Oakley rescheduling entry
    out = extract_relevant_briefing("Update my 1:1 with Oakley", _MOCK_BRIEFING)
    results.record("extract_relevant_briefing", "briefing-oakley-1on1",
                   "response-oakley-resched" in out and "oakley.stavros" in out,
                   expected="oakley rescheduling entry",
                   actual=out[:200] if out else "(empty)")

    # Test 3: Harper poll: finds the program review but NO poll URL (it has none)
    out = extract_relevant_briefing("Fill out the poll from Harper", _MOCK_BRIEFING)
    has_program_review = "response-program-review" in out
    no_poll_url = "Poll URL" not in out or "timeful" not in out.split("response-program-review")[0] if has_program_review else True
    results.record("extract_relevant_briefing", "briefing-harper-no-poll",
                   has_program_review and no_poll_url,
                   expected="harper entry WITHOUT poll URL",
                   actual=out[:200] if out else "(empty)")

    # Test 4: Generic message — minimal or empty extract
    out = extract_relevant_briefing("What is on my calendar today?", _MOCK_BRIEFING)
    has_no_obligations = "RELEVANT OBLIGATIONS" not in out
    results.record("extract_relevant_briefing", "briefing-generic-minimal",
                   has_no_obligations,
                   expected="no obligation matches",
                   actual=out[:200] if out else "(empty)")

    # Test 5: Ash doc: should find Ash Ravensworth entry
    out = extract_relevant_briefing("Reply to Ash about the doc", _MOCK_BRIEFING)
    results.record("extract_relevant_briefing", "briefing-ash-reply",
                   "response-ash-doc" in out and "ravensworth" in out.lower(),
                   expected="ash ravensworth entry",
                   actual=out[:200] if out else "(empty)")

    # Test 6: Cam 1:1: should find Cam rescheduling, include availability
    out = extract_relevant_briefing("Update the 1:1 meeting that week of Mon Apr 13 with Cam",
                                     _MOCK_BRIEFING)
    results.record("extract_relevant_briefing", "briefing-cam-sched",
                   "response-cam-resched" in out and "AVAILABILITY" in out,
                   expected="cam entry + availability data",
                   actual=out[:300] if out else "(empty)")

    # Test 7: Empty briefing: should return empty
    out = extract_relevant_briefing("Do something", {})
    results.record("extract_relevant_briefing", "briefing-empty",
                   out == "",
                   expected="empty string",
                   actual=repr(out[:50]))


# -------------------------------------------------------------------
# Skill detection and MCP decision tests
# -------------------------------------------------------------------

def run_skill_detection_tests(cases: list[dict], results: Results, functions: set[str] | None):
    """Test detect_skills for cases that have an expected 'skills' field."""
    if functions is not None and "detect_skills" not in functions:
        return

    for case in cases:
        exp_skills = case["expected"].get("skills")
        if exp_skills is None:
            continue

        cid = case["id"]
        msg = case["message"]
        is_thread = case.get("is_thread_reply", False)
        notes = case.get("notes", "")

        actual = detect_skills(msg, is_thread)
        ok = actual == exp_skills
        results.record(
            "detect_skills", cid, ok,
            expected=exp_skills,
            actual=actual,
            message=notes,
            verbose_info=msg,
        )


def run_needs_mcp_tests(cases: list[dict], results: Results, functions: set[str] | None):
    """Test needs_mcp for cases that have an expected 'needs_mcp' field."""
    if functions is not None and "needs_mcp" not in functions:
        return

    for case in cases:
        exp_mcp = case["expected"].get("needs_mcp")
        if exp_mcp is None:
            continue

        cid = case["id"]
        msg = case["message"]
        is_thread = case.get("is_thread_reply", False)
        notes = case.get("notes", "")

        modules = detect_needed_modules(msg)
        actual = needs_mcp(msg, modules, is_thread)
        ok = actual == exp_mcp
        results.record(
            "needs_mcp", cid, ok,
            expected=exp_mcp,
            actual=f"{actual} (modules={sorted(modules)})",
            message=notes,
            verbose_info=msg,
        )


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EA prompt logic regression tests")
    parser.add_argument("--tag", help="Run only cases with this tag")
    parser.add_argument("--function", help="Run only tests for this function "
                        "(detect_needed_modules, context_routing, "
                        "build_claude_md, detect_skills, needs_mcp)")
    parser.add_argument("--tier", choices=["hard", "medium", "easy"],
                        help="Run only cases in this difficulty tier")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show message text on failures")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--id", help="Run only the case with this ID")
    args = parser.parse_args()

    with open(CASES_FILE) as f:
        data = json.load(f)

    cases = data["cases"]
    bm_cases = data.get("build_claude_md_cases", [])

    # Filter by tier
    if args.tier:
        cases = [c for c in cases if c.get("tier") == args.tier]
        if not cases:
            print(f"No cases with tier '{args.tier}'")
            sys.exit(1)

    # Filter by tag
    if args.tag:
        cases = [c for c in cases if args.tag in c.get("tags", [])]
        if not cases:
            print(f"No cases with tag '{args.tag}'")
            sys.exit(1)

    # Filter by ID
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
        bm_cases = [c for c in bm_cases if c["id"] == args.id]
        if not cases and not bm_cases:
            print(f"No case with id '{args.id}'")
            sys.exit(1)

    # Filter by function
    functions = {args.function} if args.function else None

    results = Results()

    if not args.json:
        total = len(cases) + len(bm_cases)
        print(f"EA Prompt Logic Tests ({total} cases)")
        print("=" * 60)

    # Run tests
    run_case_tests(cases, results, functions)
    run_build_claude_md_tests(bm_cases, results, functions)
    run_briefing_extract_tests(results, functions)
    run_skill_detection_tests(cases, results, functions)
    run_needs_mcp_tests(cases, results, functions)

    # Output
    if args.json:
        print(json.dumps(results.to_json(), indent=2))
    else:
        results.print_scorecard(verbose=args.verbose)

    # Exit code
    sys.exit(1 if results.failures else 0)



def test_a_calendar_question_loads_the_scheduling_guide():
    """"Do you see my lab meeting on Thursday?" matched no scheduling
    phrase, so the turn ran without the guide, the only text that names
    the verbs that read a day's events; it tried verbs that do not exist
    and answered that it could not list a day. A question about the
    calendar is the `calendar` module, which loads that guide. A calendar
    write, or a question about something else, still does not."""
    for msg in ("Do you see my lab meeting this Thursday 11/12?",
                "Am I free at 11:30 AM on 10/29?",
                "What is on my calendar tomorrow?",
                "anything on my schedule Friday afternoon?",
                "what's my day look like",
                "can you see the dentist appointment next week"):
        modules = detect_needed_modules(msg)
        assert "calendar" in modules, msg
        assert "<!-- topic guide: docs/ea/scheduling.md -->" in topic_text(modules), msg
    for msg in ("put it on my calendar for Monday", "what did Lior say?",
                "file this under the minor project", "do you have the receipt?"):
        modules = detect_needed_modules(msg)
        assert not modules & {"calendar", "scheduling"}, msg


def test_a_calendar_question_is_not_a_scheduling_turn():
    """The calendar patterns are loose: a question about mail that names a
    meeting, a call or a schedule matches them too. A message whose only
    module is `scheduling` runs on the scheduling prompt, which sends any
    other request back to the EA channel, and `scheduling` also re-ranks
    the briefing extract and turns the MCP servers off. So the patterns
    are their own module: it adds the guide and leaves the turn as it
    was. A scheduling message keeps the one-module set it has without
    them, and a thread is about meeting times by the scheduling patterns
    only."""
    for msg in ("did you see the email about the faculty meeting?",
                "do you have the notes from yesterday's call?",
                "did you see the call for proposals from the program office?",
                "what is my schedule for submitting the report",
                "is there anything urgent on the schedule of deliverables",
                "What is on my calendar tomorrow?",
                "Am I free at 11:30 AM on 10/29?"):
        modules = detect_needed_modules(msg)
        assert "scheduling" not in modules, msg
        assert needs_mcp(msg, modules) == needs_mcp(msg, modules - {"calendar"}), msg
    for msg in ("am I free Friday at 2 to meet with Sam?",
                "what's on my calendar Tuesday? find a time for a quick call with Ada"):
        assert detect_needed_modules(msg) == {"scheduling"}, msg
    plain = extract_relevant_briefing("anything I should know before lunch?", _MOCK_BRIEFING)
    asked = extract_relevant_briefing("What is on my calendar today?", _MOCK_BRIEFING)
    assert ("RELEVANT OBLIGATIONS" in asked) == ("RELEVANT OBLIGATIONS" in plain)
    card = "[EA]: Am I free then? Here is what is on my calendar for the week."
    assert "scheduling" not in detect_needed_modules("write a reply saying thanks", card)


def test_a_meeting_yes_loads_the_scheduling_guide():
    """"Write to Hale saying it would be great to touch base" is a
    drafting request whose reply must carry times; so is a drafting
    request in a thread that is about meeting someone, even when the
    message itself says only "write a reply"."""
    assert "scheduling" in detect_needed_modules("write an email to Hale saying thanks for the intro and it would be great to touch base")
    plain = detect_needed_modules("write a reply to Lior saying thanks for the intro")
    assert "drafting" in plain and "scheduling" not in plain
    ctx = "[EA]: Lior Okonkwo is asking if you want to meet with Hale Zabriskie to discuss the proposal."
    assert "scheduling" in detect_needed_modules("write a reply to Lior saying thanks for the intro", ctx)
    # The thread adds nothing to a message that drafts nothing.
    assert "scheduling" not in detect_needed_modules("what did Lior say?", ctx)


def test_a_follow_up_about_the_offered_times_keeps_the_scheduling_guide():
    """A reply to a Send card that offers times ("that's Thanksgiving week,
    it won't work") had no module, so the turn found new times without the
    scheduling guide or the user's scheduling rules. A thread with two
    clock times is about meeting times; a follow-up about days, weeks or
    the times themselves in it loads the guide."""
    card = ("[EA]: Draft to Quill re: materials. Body: Would Nov 23, 10:30 to 11:00 am MT, "
            "or Nov 24, 10:45 to 11:15 am MT work for you?")
    for msg in ("That's the week of thanksgiving. It's actually not going to work.",
                "Offer Wren's slots too", "Tuesday is better", "can you do something earlier?"):
        assert "scheduling" in detect_needed_modules(msg, card), msg
    # Not a word about the times, or a thread with no times in it: nothing.
    assert "scheduling" not in detect_needed_modules("file this under the minor project", card)
    assert "scheduling" not in detect_needed_modules("That won't work.", "[EA]: The budget memo is ready.")
    assert "scheduling" not in detect_needed_modules("Tuesday is better", "[EA]: Lunch at 12 noon is fine.")
    # Ordinary words in a thread about times are not a follow-up about them.
    for msg in ("mark 3 of them closed", "decide 3 things for me", "we sat on it for a while",
                "close the first one, I did that today", "send it to Bo instead",
                "how many times did he write?", "note it for the record"):
        assert "scheduling" not in detect_needed_modules(msg, card), msg


def test_room_scheduling_asks_are_pure_scheduling():
    """The bot sends a room ask to the scheduling pipeline only when the
    modules are exactly {"scheduling"}; these everyday asks must get there,
    and asks about other work must not pick up scheduling."""
    for msg in ("Follow up with Bo with times that work next week",
                "Handle the scheduling request from Oakley",
                "Put the NIST meeting on my own calendar",
                "Give Sol my full availability"):
        assert detect_needed_modules(msg) == {"scheduling"}, msg
    for msg in ("reply to Bo about the budget",
                "what's on the agenda for the review",
                "draft a note to Sol thanking her",
                "put the report in the shared drive",
                "put it on my calendar for Monday",
                "send 2 but delete the April 6 availability paragraph",
                "Archive all of the scheduling emails that are handled"):
        assert "scheduling" not in detect_needed_modules(msg), msg


def test_install_site_names_route_to_the_sites_guide(tmp_path, monkeypatch):
    """A message that names one of the install's own sites (its slug, the
    slug's course number, or its host) loads the sites guide; the words come
    from config/github.json, not from code."""
    from preprocessor import prompt_logic as pl
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "github.json").write_text(json.dumps({"sites": {
        "_doc": "ignored",
        "zz-course-4321": {"title": "A course", "repo": "o/r",
                           "url": "https://zzcourse.example.io/"},
    }}))
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    pl._site_keywords.cache_clear()
    try:
        assert "sites" in detect_needed_modules("add the 4321 syllabus link")
        assert "sites" in detect_needed_modules("fix zzcourse.example.io")
        assert "sites" in detect_needed_modules("zz course 4321 needs a page")
        assert "sites" not in detect_needed_modules("lunch on tuesday")
    finally:
        pl._site_keywords.cache_clear()

if __name__ == "__main__":
    main()

