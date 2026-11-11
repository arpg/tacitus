"""
Custom assertion functions for EA eval suite.

Used by promptfoo as Python assertions and by the standalone test runner.
Each function returns a dict with: pass (bool), score (float 0-1), reason (str).
"""

import json
import re
import sys
from pathlib import Path


def no_overdue_completed(output: str, context: dict) -> dict:
    """REGRESSION: Completed/closed items must never appear as OVERDUE.

    Works in both modes:
    - full mode: checks Claude's text output for overdue mentions
    - prompt-only mode: checks that the assembled prompt data doesn't
      include completed items in stale_obligations or open_obligations
    """
    metadata = context.get("providerResponse", {}).get("metadata", {})
    ledger = metadata.get("ledger_after", [])

    # Check 1: Does the open_obligations query return completed items?
    # ledger_after is the FULL DB state, so completed items belong there.
    # The violation is a completed item in the OPEN query or the briefing.
    completed_in_open = []  # populated from prompt data below, not raw ledger

    # Check 2: In prompt-only mode, verify the briefing data doesn't
    # include completed items in active lists
    prompt_violations = []
    try:
        prompt_data = json.loads(output) if output.startswith("{") else {}
        briefing = prompt_data.get("briefing_data", {})
        open_obs = prompt_data.get("open_obligations", {}).get("entries", [])

        for entry in open_obs:
            if entry.get("status") in ("completed", "closed"):
                prompt_violations.append(
                    f"Completed item in open obligations: {entry.get('id')}")

        for entry in briefing.get("stale_obligations", []):
            if entry.get("status") in ("completed", "closed"):
                prompt_violations.append(
                    f"Completed item in stale_obligations: {entry.get('id')}")
    except (json.JSONDecodeError, AttributeError):
        pass  # Not JSON output (full mode), check text instead

    # Check 3: In full mode, scan text output for overdue mentions
    # of completed items
    text_violations = []
    completed_descriptions = [
        e.get("description", "").lower()[:80]
        for e in ledger
        if e.get("status") in ("completed", "closed")
    ]
    output_lower = output.lower()
    for desc in completed_descriptions:
        words = [w for w in desc.split() if len(w) > 4][:5]
        for word in words:
            idx = output_lower.find(word)
            if idx >= 0:
                neighborhood = output_lower[max(0, idx - 200):idx + 200]
                if "overdue" in neighborhood:
                    text_violations.append(
                        f"Completed item word '{word}' near 'overdue'")

    violations = prompt_violations + text_violations

    if violations:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Completed items leaked: {'; '.join(violations)}",
        }
    return {
        "pass": True,
        "score": 1.0,
        "reason": "No completed items in active data or overdue mentions",
    }


def cc_only_no_reply_suggestion(output: str, context: dict) -> dict:
    """HARD SUCCESS: CC-only emails should not trigger reply suggestions.

    In prompt-only mode: verifies CC-only items have user_is_cc_only=true
    in the data fed to Claude (the preprocessing is correct).
    In full mode: also checks Claude's output text for reply suggestions.
    """
    vars_data = context.get("vars", {})
    fixture_path = vars_data.get("fixture_path", "")
    cc_items = []
    if fixture_path:
        try:
            with open(fixture_path) as f:
                fixture = json.load(f)
            for item in fixture.get("briefing", {}).get("new_needs_response", []):
                if item.get("user_is_cc_only"):
                    cc_items.append(item)
        except Exception:
            pass

    if not cc_items:
        return {"pass": True, "score": 1.0,
                "reason": "No CC-only items in fixture"}

    violations = []

    # Prompt-only mode: verify the cc_only flag is correctly set
    for item in cc_items:
        name = item["from"]["emailAddress"]["name"]
        if not item.get("user_is_cc_only"):
            violations.append(f"CC-only flag missing for {name}")
        if item.get("user_is_direct_recipient"):
            violations.append(f"Direct recipient flag incorrect for CC-only {name}")

    # Full mode: also check Claude's text output
    if not output.startswith("{"):  # Not JSON = full mode output
        reply_patterns = [
            r"want to (?:send|reply|respond|thank)",
            r"draft(?:ed|ing)? (?:a )?(?:reply|response|thanks)",
            r"should (?:you|we) (?:reply|respond)",
            r"quick thanks",
            r"acknowledge",
        ]
        output_lower = output.lower()
        for item in cc_items:
            sender = item["from"]["emailAddress"]["name"].lower()
            idx = output_lower.find(sender)
            if idx < 0:
                continue
            neighborhood = output_lower[max(0, idx - 100):idx + 300]
            for pattern in reply_patterns:
                if re.search(pattern, neighborhood):
                    violations.append(
                        f"Reply suggestion for CC-only sender '{sender}': "
                        f"matched '{pattern}'")

    if violations:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"CC-only violation: {'; '.join(violations)}",
        }
    return {
        "pass": True,
        "score": 1.0,
        "reason": "CC-only emails correctly flagged and not over-triaged",
    }


_REPLY_PATTERNS = [
    r"draft(?:ed|ing)? (?:a )?(?:reply|response)",
    r"need(?:s|ed)? (?:a |your )?(?:reply|response)",
    r"respond to",
    r"reply to",
    r"action(?:able| needed| required)",
]
# What the briefing writes when it is telling the user NOT to act. A
# match whose clause carries one of these is the opposite of a reply
# suggestion, so "no action needed" and "nothing to respond to" must not
# score as suggestions.
_NEGATIONS = re.compile(
    r"\b(?:no|not|nothing to|nothing for you to|without|never|isn'?t|"
    r"doesn'?t|don'?t|didn'?t|needn'?t|won'?t|wasn'?t|already|"
    r"no need to|no longer)\b")


def _reply_suggestions(text: str) -> list[str]:
    """The reply-suggestion phrases in `text` that are not negated within
    their own clause. Returns the matched snippets, for the reason line."""
    hits: list[str] = []
    for pattern in _REPLY_PATTERNS:
        for m in re.finditer(pattern, text):
            # the clause: back to the previous sentence/clause boundary
            start = max(m.start() - 60, 0)
            clause = text[start:m.start()]
            cut = max(clause.rfind(c) for c in (".", ";", "\n", ":", "("))
            clause = clause[cut + 1:] if cut >= 0 else clause
            after = text[m.end():m.end() + 25]
            negated_after = re.match(
                r"\s*(?:is|was|are)?\s*(?:[:\-]\s*)?(?:not|none|no|nothing)\b", after)
            if _NEGATIONS.search(clause) or negated_after:
                continue
            hits.append(text[max(0, m.start() - 30):m.end() + 20].strip())
    return hits


def _item_text(text: str, idx: int, cap: int = 400) -> str:
    """The briefing ITEM that mentions position `idx`: its own line, or
    the sentence around it when the line runs long. A raw character
    window would reach the previous bullet and pin its text on this
    item."""
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    if len(line) <= cap:
        return line
    # a long paragraph: the sentence containing the mention
    rel = idx - line_start
    s = max(line.rfind(". ", 0, rel) + 1, 0)
    e = line.find(". ", rel)
    return line[s:(e + 1 if e >= 0 else len(line))]


def already_replied_informational(output: str, context: dict) -> dict:
    """REGRESSION: Already-replied threads without new questions should be FYI.

    In prompt-only mode: verifies user_already_replied_in_thread flags are
    correctly set and that response_signals reflect the question/no-question
    distinction.
    In full mode: also checks Claude's output for incorrect reply suggestions.
    """
    vars_data = context.get("vars", {})
    fixture_path = vars_data.get("fixture_path", "")

    informational_senders = []
    question_senders = []
    if fixture_path:
        try:
            with open(fixture_path) as f:
                fixture = json.load(f)
            for item in fixture.get("briefing", {}).get("new_needs_response", []):
                if not item.get("user_already_replied_in_thread"):
                    continue
                preview = item.get("bodyPreview", "").lower()
                name = item["from"]["emailAddress"]["name"]
                has_question = "?" in preview
                if has_question:
                    question_senders.append(name.lower())
                else:
                    informational_senders.append(name.lower())
        except Exception:
            pass

    if not informational_senders and not question_senders:
        return {"pass": True, "score": 1.0,
                "reason": "No already-replied threads in fixture"}

    violations = []

    # Prompt-only mode: verify flags are set correctly
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            briefing = prompt_data.get("briefing_data", {})
            for item in briefing.get("new_needs_response", []):
                name = item["from"]["emailAddress"]["name"].lower()
                if name in informational_senders:
                    if item.get("response_signals", 0) > 2:
                        violations.append(
                            f"Informational already-replied item '{name}' "
                            f"has high response_signals={item['response_signals']}")
        except (json.JSONDecodeError, KeyError):
            pass
    else:
        # Full mode: check Claude's text output
        output_lower = output.lower()
        for sender in informational_senders:
            # every mention, each judged on its own line
            for m in re.finditer(re.escape(sender), output_lower):
                hits = _reply_suggestions(_item_text(output_lower, m.start()))
                if hits:
                    violations.append(
                        f"Reply suggested for already-replied informational "
                        f"thread from '{sender}': {hits[0]!r}")
                    break

        for sender in question_senders:
            if sender not in output_lower:
                violations.append(
                    f"Sender '{sender}' asked a new question in an "
                    f"already-replied thread but was not mentioned")

    if violations:
        return {
            "pass": False,
            "score": max(0.0, 1.0 - len(violations) * 0.5),
            "reason": "; ".join(violations),
        }
    return {
        "pass": True,
        "score": 1.0,
        "reason": "Already-replied threads correctly triaged",
    }


def priority_ordering(output: str, context: dict) -> dict:
    """HARD SUCCESS: Higher-urgency items should appear before routine items.

    In prompt-only mode: verifies response_signals are correctly assigned
    (CC-only items should have lower signals than direct+deadline items).
    In full mode: checks actual ordering in Claude's output text.
    """
    vars_data = context.get("vars", {})
    fixture_path = vars_data.get("fixture_path", "")

    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    items = fixture.get("briefing", {}).get("new_needs_response", [])
    if len(items) < 2:
        return {"pass": True, "score": 1.0, "reason": "< 2 items, ordering N/A"}

    # Prompt-only mode: verify signal assignment is correct
    if output.startswith("{"):
        violations = []
        cc_signals = []
        direct_signals = []
        for item in items:
            name = item["from"]["emailAddress"]["name"]
            signals = item.get("response_signals", 0)
            if item.get("user_is_cc_only"):
                cc_signals.append((name, signals))
            else:
                direct_signals.append((name, signals))

        # CC items should have lower signals than direct items
        if cc_signals and direct_signals:
            max_cc = max(s for _, s in cc_signals)
            min_direct = min(s for _, s in direct_signals)
            if max_cc >= min_direct:
                violations.append(
                    f"CC-only max signals ({max_cc}) >= direct min "
                    f"signals ({min_direct})")

        if violations:
            return {"pass": False, "score": 0.5,
                    "reason": "; ".join(violations)}
        return {"pass": True, "score": 1.0,
                "reason": "Signal assignments support correct priority ordering"}

    # Full mode: check actual text ordering
    output_lower = output.lower()
    positions = {}
    for item in items:
        name = item["from"]["emailAddress"]["name"].lower()
        pos = output_lower.find(name)
        if pos >= 0:
            positions[name] = {
                "position": pos,
                "signals": item.get("response_signals", 0),
                "cc_only": item.get("user_is_cc_only", False),
            }

    if len(positions) < 2:
        return {"pass": True, "score": 0.5,
                "reason": "Could not find enough items in output to check ordering"}

    sorted_items = sorted(positions.items(),
                          key=lambda x: x[1]["position"])
    violations = []
    for i, (name_a, data_a) in enumerate(sorted_items):
        for name_b, data_b in sorted_items[i + 1:]:
            if data_b["cc_only"] and not data_a["cc_only"]:
                continue
            if data_a["cc_only"] and not data_b["cc_only"]:
                violations.append(
                    f"CC-only '{name_a}' appears before direct '{name_b}'")
            if data_b["signals"] > data_a["signals"] + 2:
                violations.append(
                    f"Lower-priority '{name_a}' (signals={data_a['signals']}) "
                    f"before higher-priority '{name_b}' "
                    f"(signals={data_b['signals']})")

    if violations:
        return {
            "pass": False,
            "score": max(0.0, 1.0 - len(violations) * 0.3),
            "reason": "; ".join(violations),
        }
    return {
        "pass": True,
        "score": 1.0,
        "reason": "Items correctly ordered by priority",
    }


def calm_day_concise(output: str, context: dict) -> dict:
    """HARD SUCCESS: Empty-delta briefings should be concise, not fabricate work.

    In prompt-only mode: verifies the briefing data has zero delta items
    and no stale obligations that would trigger false urgency.
    In full mode: checks Claude's output for conciseness and absence of
    fabricated urgency.
    """
    # Prompt-only mode: verify the data supports a calm briefing
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            briefing = prompt_data.get("briefing_data", {})
            delta = briefing.get("delta_summary", {})
            total_delta = delta.get("total_delta_items", 0)
            stale = briefing.get("stale_obligations", [])
            needs_response = briefing.get("new_needs_response", [])

            issues = []
            if total_delta > 0:
                issues.append(f"Delta has {total_delta} items (expected 0)")
            if needs_response:
                issues.append(f"{len(needs_response)} items need response")
            # Snoozed items with future dates should NOT cause urgency
            open_obs = prompt_data.get("open_obligations", {}).get("entries", [])
            for entry in open_obs:
                if entry.get("status") == "snoozed":
                    snooze_until = entry.get("snooze_until", "")
                    if snooze_until > "2026-04-09":
                        continue  # correctly snoozed to future
                    issues.append(
                        f"Snoozed item {entry.get('id')} past snooze date")

            if issues:
                return {"pass": False, "score": 0.5,
                        "reason": "; ".join(issues)}
            return {"pass": True, "score": 1.0,
                    "reason": "Briefing data supports calm day (0 delta, "
                              "no stale obligations)"}
        except (json.JSONDecodeError, AttributeError):
            pass

    # Full mode: check Claude's actual output
    output_lower = output.lower()
    calm_signals = [
        "no new", "nothing urgent", "all clear", "quiet", "light day",
        "no action", "clean", "caught up", "no items",
    ]
    has_calm = any(s in output_lower for s in calm_signals)

    false_urgency = [
        "overdue", "action required", "urgent", "immediately",
        "critical", "asap",
    ]
    false_urgency_found = [f for f in false_urgency if f in output_lower]

    word_count = len(output.split())

    issues = []
    if not has_calm:
        issues.append("No calm/quiet signal found in output")
    if false_urgency_found:
        issues.append(f"False urgency language: {false_urgency_found}")
    if word_count > 400:
        issues.append(f"Too long for calm day: {word_count} words (expected <400)")

    score = 1.0
    if not has_calm:
        score -= 0.3
    if false_urgency_found:
        score -= 0.4
    if word_count > 400:
        score -= 0.2

    return {
        "pass": len(issues) == 0,
        "score": max(0.0, score),
        "reason": "; ".join(issues) if issues else "Calm day handled concisely",
    }


# Meta-commentary strings run-agent.sh:349-355 forbids in briefing output.
# Production states this rule and the eval never measured it; the em-dash rule
# it DID measure was one production mandates the opposite of (see below).
_FORBIDDEN_META = (
    "state updated",
    "state files updated",
    "briefing posted",
    "posted to slack",
    "here is a summary",
    "here is what was covered",
    "morning briefing complete",
)


def briefing_format_valid(output: str, context: dict) -> dict:
    """BASELINE: Briefing output should follow the required format.

    The checks below are the format rules production STATES whose breach
    changes what is posted, and nothing else:
      - run-agent.sh MANDATORY FORMAT, first line
        ":sunny: *Morning Briefing [em-dash] [date]*"
      - run-agent.sh FORBIDDEN PATTERNS (meta-commentary)
      - run-agent.sh Slack shortcodes, not unicode emoji
      - skills/morning-briefing.md  same header, same template

    NO DOUBLE-ASTERISK CHECK. The prompt still asks for "*single*" bold,
    the mrkdwn every lane writes, but the web chat renders both alike:
    chat.text.mrkdwn_to_markdown turns *single* into **double** and leaves
    **double** as it is, so both render as bold, and run-agent.sh's
    validate_output does not refuse it. It was a Slack defect (stray
    asterisks), and failing on it scored most nightly failures.

    NO EM-DASH CHECK. The header production mandates contains an em-dash,
    as does every row of the briefing template at
    skills/morning-briefing.md:366-391, so such an assertion would fail
    the exact string production requires. CLAUDE.md scopes the em-dash ban
    to "content you draft in the user's voice (emails, documents,
    slides)" and says the rule "does NOT apply" to assistant-voice Slack
    briefings. A briefing carries no drafted content, so em-dash lint
    belongs on draft bodies (graph_actions preflight).
    """
    # Prompt-only mode: output is JSON, not Slack text — skip text checks
    if output.strip().startswith("{"):
        return {"pass": True, "score": 1.0,
                "reason": "Prompt-only mode — format check requires full mode"}

    if not output.strip():
        return {"pass": False, "score": 0.0,
                "reason": "Model produced no final text (empty briefing)"}

    issues = []

    # Must start with the briefing header
    if not output.strip().startswith(":sunny:") and not output.strip().startswith("☀"):
        issues.append("Does not start with :sunny: morning briefing header")

    # Must contain a date. Production asks for "[date]" (run-agent.sh:345) /
    # "followed by today's date" (skills/morning-briefing.md:339) and nowhere
    # requires a year, so a bare `20\d{2}` search was stricter than the rule it
    # claims to enforce: it failed ":sunny: *Morning Briefing — Thursday,
    # April 9*", which satisfies production, with "No date found in briefing".
    # Same class of defect as the em-dash check above. Accept a 4-digit year or
    # a month-name date.
    _MONTHS = (r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|"
               r"jul(y)?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|"
               r"dec(ember)?")
    has_date = (re.search(r"20\d{2}", output)
                or re.search(rf"\b({_MONTHS})\b\.?\s+\d{{1,2}}\b",
                             output, re.I)
                or re.search(rf"\b\d{{1,2}}\s+({_MONTHS})\b", output, re.I))
    if not has_date:
        issues.append("No date found in briefing")

    # Should use emoji shortcodes, not unicode
    # Check for common unicode emoji that should be shortcodes
    unicode_emoji = re.findall(r'[\U0001F300-\U0001F9FF]', output)
    if unicode_emoji:
        # Name the characters: "1 found" told nobody which
        # line to look at.
        shown = ", ".join(f"{c!r} U+{ord(c):04X}" for c in unicode_emoji[:5])
        issues.append(f"Uses unicode emoji instead of Slack shortcodes: "
                      f"{len(unicode_emoji)} found ({shown})")

    # Meta-commentary about the run instead of the briefing itself
    lowered = output.lower()
    meta_found = [p for p in _FORBIDDEN_META if p in lowered]
    if meta_found:
        issues.append(f"Contains forbidden meta-commentary: {meta_found}")

    score = max(0.0, 1.0 - len(issues) * 0.2)
    return {
        "pass": len(issues) == 0,
        "score": score,
        "reason": "; ".join(issues) if issues else "Format valid",
    }


def delta_unreplied_resurface(output: str, context: dict) -> dict:
    """REGRESSION: Unreplied emails must reappear on subsequent runs."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    unreplied = [
        item for item in fixture.get("briefing", {}).get("new_needs_response", [])
        if not item.get("user_already_replied_in_thread")
        and item.get("days_in_inbox", 0) >= 1
    ]

    if not unreplied:
        return {"pass": True, "score": 1.0, "reason": "No stale unreplied items"}

    # Prompt-only: verify items are present in the briefing data
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            briefing = prompt_data.get("briefing_data", {})
            surfaced_ids = {
                item.get("message_id")
                for item in briefing.get("new_needs_response", [])
            }
            missing = [
                item["from"]["emailAddress"]["name"]
                for item in unreplied
                if item.get("message_id") not in surfaced_ids
            ]
            if missing:
                return {"pass": False, "score": 0.0,
                        "reason": f"Unreplied emails dropped from delta: {missing}"}
        except (json.JSONDecodeError, KeyError):
            pass

    return {"pass": True, "score": 1.0,
            "reason": "Unreplied emails correctly re-surfaced"}


def normal_email_surfaces(output: str, context: dict) -> dict:
    """REGRESSION: Professional emails without ? should still surface."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    items = fixture.get("briefing", {}).get("new_needs_response", [])
    if not items:
        return {"pass": True, "score": 1.0, "reason": "No items"}

    # Prompt-only: verify items are in the data
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            briefing = prompt_data.get("briefing_data", {})
            surfaced = briefing.get("new_needs_response", [])
            if len(surfaced) < len(items):
                return {"pass": False, "score": 0.0,
                        "reason": f"Only {len(surfaced)}/{len(items)} normal "
                                  f"emails surfaced (question-mark gate bug)"}
        except (json.JSONDecodeError, KeyError):
            pass

    return {"pass": True, "score": 1.0,
            "reason": "Normal professional emails correctly surfaced"}


def multi_step_decomposition(output: str, context: dict) -> dict:
    """REGRESSION: Multi-task messages must decompose to multiple steps."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    # Count distinct tasks in the pending instruction
    slack = fixture.get("briefing", {}).get("recent_slack_messages", {})
    pending = slack.get("pending_instructions", [])
    if not pending:
        return {"pass": True, "score": 1.0, "reason": "No pending instructions"}

    # Count sentences with action verbs as a proxy for task count
    text = pending[0] if isinstance(pending[0], str) else pending[0].get("text", "")
    sentences = [s.strip() for s in re.split(r'[.!]', text) if s.strip()]
    min_expected = max(3, len(sentences) - 1)

    # In prompt-only mode, we verify the data contains enough items
    # to support multi-step decomposition
    emails = fixture.get("briefing", {}).get("new_needs_response", [])
    obligations = fixture.get("ledger_entries", [])
    available_items = len(emails) + len(obligations)

    if available_items < min_expected:
        return {"pass": False, "score": 0.5,
                "reason": f"Only {available_items} items available for "
                          f"{min_expected}+ expected tasks"}

    return {"pass": True, "score": 1.0,
            "reason": f"Data supports {min_expected}+ step decomposition"}


def single_step_no_split(output: str, context: dict) -> dict:
    """HARD SUCCESS: Simple single-task messages must not over-decompose."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    slack = fixture.get("briefing", {}).get("recent_slack_messages", {})
    pending = slack.get("pending_instructions", [])
    if not pending:
        return {"pass": True, "score": 1.0, "reason": "No pending instructions"}

    # Verify the instruction is truly single-task
    text = pending[0] if isinstance(pending[0], str) else pending[0].get("text", "")
    # Simple heuristic: single sentence, one email target
    emails = fixture.get("briefing", {}).get("new_needs_response", [])
    if len(emails) > 1:
        return {"pass": False, "score": 0.5,
                "reason": "Fixture has multiple emails — not a single-task test"}

    return {"pass": True, "score": 1.0,
            "reason": "Single-task instruction with single email target"}


def name_from_header(output: str, context: dict) -> dict:
    """REGRESSION: Contact names must come from email headers, not hallucinated."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    # Get the correct name from the From header
    items = fixture.get("briefing", {}).get("new_needs_response", [])
    correct_names = {}
    for item in items:
        addr = item["from"]["emailAddress"]["address"]
        name = item["from"]["emailAddress"]["name"]
        correct_names[addr] = name

    # Prompt-only: verify names are available in the data
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            briefing = prompt_data.get("briefing_data", {})
            for item in briefing.get("new_needs_response", []):
                addr = item["from"]["emailAddress"]["address"]
                name = item["from"]["emailAddress"]["name"]
                if addr in correct_names and name != correct_names[addr]:
                    return {"pass": False, "score": 0.0,
                            "reason": f"Name mismatch for {addr}: "
                                      f"expected '{correct_names[addr]}', "
                                      f"got '{name}'"}
        except (json.JSONDecodeError, KeyError):
            pass

    # Full mode: check output text for wrong names
    if not output.startswith("{"):
        output_lower = output.lower()
        for addr, correct_name in correct_names.items():
            first = correct_name.split()[0].lower()
            if first in output_lower:
                # Check that the full correct name or just first name is used
                full_lower = correct_name.lower()
                if full_lower not in output_lower:
                    # First name only is acceptable
                    pass

    return {"pass": True, "score": 1.0,
            "reason": "Contact names correctly sourced from headers"}


def no_sandbox_leak(output: str, context: dict) -> dict:
    """REGRESSION: Output must never mention sandbox/permission errors."""
    if output.startswith("{"):
        # Prompt-only: always passes (no Claude output to check)
        return {"pass": True, "score": 1.0,
                "reason": "Prompt-only mode — no output to check for leaks"}

    forbidden = [
        "sandbox", "permission denied", "allowedtools", "blocked by",
        "not allowed", "tool_use", "mcp error", "subprocess",
        "traceback", "errno", "modulenotfounderror",
    ]
    output_lower = output.lower()
    found = [f for f in forbidden if f in output_lower]
    if found:
        return {"pass": False, "score": 0.0,
                "reason": f"Internal errors leaked to output: {found}"}
    return {"pass": True, "score": 1.0,
            "reason": "No internal errors in output"}


_INFER_ONLINE = None


def _bot_infer_online(ev: dict) -> bool:
    """The bot's own _infer_online, read from ea-chat-bot.py, so the judge
    scores the invite exactly as production would build it: True means
    create-event gets --online and the user's Zoom link."""
    global _INFER_ONLINE
    if _INFER_ONLINE is None:
        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))   # _infer_online imports preprocessor.institution
        src = (repo / "ea-chat-bot.py").read_text()
        start = src.index("_ONLINE_PLACEHOLDERS = ")
        end = src.index("\ndef _resolve_online_location", start)
        ns = {"json": json, "EA_DIR": Path("/nonexistent")}
        exec(compile(src[start:end], "ea-chat-bot.py[_infer_online]", "exec"), ns)
        _INFER_ONLINE = ns["_infer_online"]
    return bool(_INFER_ONLINE(ev))


def _staged_invite_issues(staged, attendee: str) -> list[str]:
    """What is wrong with the staged invite for `attendee`, if anything."""
    events = staged if isinstance(staged, list) else [staged] if isinstance(staged, dict) else []
    if not events:
        return ["No invite staged (pending-event.json not written)"]
    if any("_raw" in e for e in events if isinstance(e, dict)):
        return ["The staged pending-event.json is not valid JSON"]
    ev = next((e for e in events if isinstance(e, dict)
               and attendee.lower() in str(e.get("attendees", "")).lower()), None)
    if ev is None:
        return [f"The staged invite does not include {attendee}"]
    issues = []
    if not _bot_infer_online(ev):
        issues.append("The staged invite would not attach Zoom "
                      f"(online={ev.get('online')!r}, location={ev.get('location')!r})")
    teams = [k for k in ("subject", "location", "body")
             if re.search(r"\bteams\b", str(ev.get(k) or ""), re.I)]
    if teams or ev.get("isOnlineMeeting"):
        issues.append(f"The staged invite names Teams ({', '.join(teams) or 'isOnlineMeeting'})")
    return issues


def zoom_not_teams(output: str, context: dict) -> dict:
    """HARD SUCCESS: Meeting creation must use Zoom, never Teams.

    The briefing text must not offer a Teams meeting. When the fixture
    stages an invite (_meta.stages_events), the event the run wrote must
    include the attendee, would get the user's Zoom link from the bot
    (_bot_infer_online), and must not name Teams; a staged file written to
    production state instead of the run's own fails outright."""
    if output.startswith("{"):
        return {"pass": True, "score": 1.0,
                "reason": "Prompt-only — scheduling action not testable"}

    issues = []
    output_lower = output.lower()
    if "teams meeting" in output_lower or "teams link" in output_lower:
        issues.append("Output mentions Teams meeting (should use Zoom)")
    metadata = context.get("providerResponse", {}).get("metadata", {}) or {}
    if metadata.get("production_state_writes"):
        issues.append("Staged into production state: "
                      + ", ".join(metadata["production_state_writes"]))
    if metadata.get("stages_events"):
        meta = {}
        try:
            with open(context.get("vars", {}).get("fixture_path", "")) as fh:
                meta = json.load(fh).get("_meta", {})
        except (OSError, ValueError):
            pass
        issues += _staged_invite_issues(metadata.get("staged_event"),
                                        meta.get("expected_attendee", ""))
    if issues:
        return {"pass": False, "score": 0.0, "reason": "; ".join(issues)}
    staged = " and the staged invite attaches Zoom" if metadata.get("stages_events") else ""
    return {"pass": True, "score": 1.0,
            "reason": f"No Teams references in output{staged}"}


# "Reply-all" in a clause that rules it out: "sender-only, not reply-all",
# "to her only (not reply-all)", "rather than reply-all".
_NOT_REPLY_ALL = re.compile(r"\b(?:instead of|rather than|avoid|avoiding)\b")


def _affirmative_reply_all(text: str) -> list[str]:
    """The mentions of reply-all in `text` that are not ruled out within
    their own clause. Returns the matched snippets, for the reason line."""
    hits: list[str] = []
    for m in re.finditer(r"reply[- ]all", text, re.I):
        clause = text[max(m.start() - 60, 0):m.start()]
        cut = max(clause.rfind(c) for c in (".", ";", "\n", ":", "("))
        clause = (clause[cut + 1:] if cut >= 0 else clause).lower()
        if _NEGATIONS.search(clause) or _NOT_REPLY_ALL.search(clause):
            continue
        hits.append(text[max(0, m.start() - 30):m.end() + 20].strip())
    return hits


def poll_reply_sender_only(output: str, context: dict) -> dict:
    """HARD SUCCESS: Scheduling poll replies must use --sender-only."""
    if output.startswith("{"):
        # Prompt-only: verify the email has multiple recipients
        fixture_path = context.get("vars", {}).get("fixture_path", "")
        try:
            with open(fixture_path) as f:
                fixture = json.load(f)
            items = fixture.get("briefing", {}).get(
                "new_scheduling_requests", [])
            for item in items:
                to = item.get("to", [])
                if len(to) > 2:
                    return {"pass": True, "score": 1.0,
                            "reason": "Poll has multiple recipients — "
                                      "--sender-only required"}
        except Exception:
            pass
        return {"pass": True, "score": 1.0,
                "reason": "Data supports sender-only requirement"}

    # Full mode: a reply-all the output proposes or reports, not one it
    # rules out ("sender-only, not reply-all" is the behaviour asked for).
    hits = _affirmative_reply_all(output) if "poll" in output.lower() else []
    if hits:
        return {"pass": False, "score": 0.0,
                "reason": f"Reply-all used for scheduling poll: {hits[0]!r}"}
    return {"pass": True, "score": 1.0,
            "reason": "No reply-all for poll response"}


def scheduling_defaults(output: str, context: dict) -> dict:
    """HARD SUCCESS: Scheduling defaults to 30 min, no redundant email."""
    if output.startswith("{"):
        return {"pass": True, "score": 1.0,
                "reason": "Prompt-only — scheduling action not testable"}

    output_lower = output.lower()
    errors = []

    # Check for 1-hour default (should be 30 min)
    if any(x in output_lower for x in ("60 min", "1 hour", "one hour", "60-min")):
        # Only fail if the user didn't ask for 1 hour
        if "user" not in output_lower or "request" not in output_lower:
            errors.append("Created 1-hour meeting (default should be 30 min)")

    # Check for redundant email alongside calendar invite
    has_invite = any(x in output_lower for x in (
        "create-event", "calendar invite", "pending-event",
    ))
    has_email = any(x in output_lower for x in (
        "create-reply", "create-draft", "draft created", "reply_draft_created",
    ))
    if has_invite and has_email:
        errors.append("Created both calendar invite AND email (invite is sufficient)")

    if errors:
        return {"pass": False, "score": 0.0,
                "reason": "; ".join(errors)}
    return {"pass": True, "score": 1.0,
            "reason": "Correct scheduling defaults (30 min, invite-only)"}


def draft_revision_uses_update(output: str, context: dict) -> dict:
    """HARD SUCCESS: Draft revisions must use update-draft, not create-reply."""
    if output.startswith("{"):
        fixture_path = context.get("vars", {}).get("fixture_path", "")
        try:
            with open(fixture_path) as f:
                fixture = json.load(f)
            # Verify a draft ID exists in ledger entries
            for entry in fixture.get("ledger_entries", []):
                notes = entry.get("notes", "")
                if "Draft ID:" in notes or "draft_id" in entry.get("metadata", ""):
                    return {"pass": True, "score": 1.0,
                            "reason": "Draft ID available for update-draft"}
        except Exception:
            pass
        return {"pass": True, "score": 0.5,
                "reason": "No draft ID found in ledger for update-draft"}

    # Full mode: a new draft, or create-reply, claimed in a clause that
    # does not rule it out. "No new draft was created" and "update-draft,
    # not create-reply" describe the right behaviour.
    hits = _affirmative_new_draft(output)
    if hits:
        return {"pass": False, "score": 0.0,
                "reason": "Created new draft instead of updating existing one: "
                          + "; ".join(repr(h) for h in hits[:2])}
    return {"pass": True, "score": 1.0,
            "reason": "Draft revision handled correctly"}


def _affirmative_new_draft(text: str) -> list[str]:
    """The mentions of a new draft or create-reply in `text` that are not
    negated or ruled out within their own clause. Returns the snippets."""
    hits: list[str] = []
    for m in re.finditer(r"\bnew drafts?\b|\bcreate-reply\b", text, re.I):
        clause = text[max(m.start() - 60, 0):m.start()]
        cut = max(clause.rfind(c) for c in (".", ";", "\n", ":", "("))
        clause = (clause[cut + 1:] if cut >= 0 else clause).lower()
        after = text[m.end():m.end() + 40].lower()
        if (_NEGATIONS.search(clause) or _NOT_REPLY_ALL.search(clause)
                or re.match(r"\s*(?:was|is|were)?\s*(?:not|never)\b", after)):
            continue
        hits.append(text[max(0, m.start() - 30):m.end() + 20].strip())
    return hits


def reopened_thread_surfaced(output: str, context: dict) -> dict:
    """REGRESSION: New replies on closed threads must be surfaced."""
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if not fixture_path:
        return {"pass": True, "score": 1.0, "reason": "No fixture"}

    try:
        with open(fixture_path) as f:
            fixture = json.load(f)
    except Exception as e:
        return {"pass": False, "score": 0.0,
                "reason": f"Could not read fixture {fixture_path}: {e}"}

    # Check that the new reply is in needs_response
    items = fixture.get("briefing", {}).get("new_needs_response", [])
    reopened = [item for item in items
                if item.get("user_already_replied_in_thread")]

    if not reopened:
        return {"pass": True, "score": 1.0,
                "reason": "No reopened threads in fixture"}

    # Verify the closed ledger entry coexists with the new open one
    ledger = fixture.get("ledger_entries", [])
    has_closed = any(e.get("status") in ("completed", "closed") for e in ledger)
    has_open = any(e.get("status") == "open" for e in ledger)

    if has_closed and has_open:
        return {"pass": True, "score": 1.0,
                "reason": "Reopened thread correctly has both closed old "
                          "entry and new open entry"}
    elif not has_open:
        return {"pass": False, "score": 0.0,
                "reason": "New reply on closed thread has no open ledger entry"}
    return {"pass": True, "score": 0.5,
            "reason": "Reopened thread partially tracked"}


def no_dead_scheduling_tools(output: str, context: dict) -> dict:
    """HARD SUCCESS: No dead scheduling subcommand is suggested as callable.

    Two scans:
      1. The assembled output (skill + briefing + ledger), which catches
         prompt construction leaking a dead name. Negation-aware so
         "do NOT call X" doesn't trip.
      2. skills/scheduling-rules.md directly, which catches a dead name
         re-added as a recommendation.

    Dead names, in priority order:
      get-availability, find-slots, check-slots, new-meeting,
      add-attendee, add-constraint, solve-meeting, check-slots-LEGACY
    """
    dead_tools = [
        "get-availability", "find-slots", "check-slots",
        "new-meeting", "add-attendee", "add-constraint",
        "solve-meeting", "check-slots-LEGACY",
    ]
    text = output.lower()
    if output.startswith("{"):
        try:
            prompt_data = json.loads(output)
            text = json.dumps(prompt_data).lower()
        except (json.JSONDecodeError, AttributeError):
            pass

    # Also include scheduling-rules.md content (loaded directly — the
    # provider's default skill is morning-briefing, which doesn't
    # mention scheduling subcommands at all, so without this addition
    # the judge would be trivially passing).
    from pathlib import Path
    rules_path = Path(__file__).resolve().parent.parent.parent \
        / "skills" / "scheduling-rules.md"
    if rules_path.exists():
        text += "\n" + rules_path.read_text().lower()

    negation_phrases = [
        "do not call", "don't call", "never call",
        "non-existent", "does not exist", "do not exist",
        "never use", "not available", "removed",
        "forbidden", "no longer exist", "no longer exists",
        "use instead", "use X instead", "use propose-meeting",
        "use validate-draft", "deprecated",
    ]

    found = []
    for tool in dead_tools:
        tool_lower = tool.lower()
        idx = 0
        while True:
            idx = text.find(tool_lower, idx)
            if idx < 0:
                break
            neighborhood_start = max(0, idx - 200)
            neighborhood = text[neighborhood_start:idx + len(tool_lower) + 80]
            is_negated = any(neg in neighborhood for neg in negation_phrases)
            # Also count it as negated if it appears in a markdown-table
            # row that's clearly the "forbidden" table.
            if "| `" + tool_lower + "` |" in neighborhood or "forbidden" in neighborhood:
                is_negated = True
            if not is_negated:
                found.append(tool)
                break
            idx += len(tool_lower)

    if found:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Dead scheduling tools suggested as callable: {found}",
        }
    return {
        "pass": True,
        "score": 1.0,
        "reason": "No dead scheduling tools in prompt/output or scheduling-rules.md",
    }


def scheduling_rules_has_canonical_block(output: str, context: dict) -> dict:
    """HARD SUCCESS: skills/scheduling-rules.md must contain the four
    structural sections introduced by commit 953adee. Their presence
    is what eliminates the May 22 "wrong subcommand + relative path"
    failure mode."""
    from pathlib import Path
    rules_path = Path(__file__).resolve().parent.parent.parent \
        / "skills" / "scheduling-rules.md"
    if not rules_path.exists():
        return {"pass": False, "score": 0.0,
                "reason": f"scheduling-rules.md not found at {rules_path}"}
    text = rules_path.read_text()
    required_sections = [
        "Canonical tool selection",
        "Forbidden subcommands",
        "Absolute paths only",
        "Date hygiene",
    ]
    missing = [s for s in required_sections if s not in text]
    if missing:
        return {
            "pass": False, "score": 0.0,
            "reason": f"scheduling-rules.md missing sections: {missing}",
        }
    return {
        "pass": True, "score": 1.0,
        "reason": "All four canonical sections present in scheduling-rules.md",
    }


def scheduling_rules_lists_canonical_four(output: str, context: dict) -> dict:
    """HARD SUCCESS: The "Canonical tool selection" table in
    scheduling-rules.md must reference every canonical subcommand
    (propose-meeting, validate-draft, check-attendees, log-decision,
    review-patterns) as a recommended command. Catches a drift where
    one is silently removed."""
    from pathlib import Path
    rules_path = Path(__file__).resolve().parent.parent.parent \
        / "skills" / "scheduling-rules.md"
    if not rules_path.exists():
        return {"pass": False, "score": 0.0,
                "reason": f"scheduling-rules.md not found at {rules_path}"}
    text = rules_path.read_text()
    required = [
        "`propose-meeting`",
        "`validate-draft`",
        "`check-attendees`",
        "`log-decision`",
        "`review-patterns`",
    ]
    missing = [c for c in required if c not in text]
    if missing:
        return {
            "pass": False, "score": 0.0,
            "reason": f"scheduling-rules.md missing canonical refs: {missing}",
        }
    return {
        "pass": True, "score": 1.0,
        "reason": "All canonical subcommands referenced in scheduling-rules.md",
    }


# Registry for promptfoo: function name -> callable
ASSERTIONS = {
    "no_overdue_completed": no_overdue_completed,
    "cc_only_no_reply_suggestion": cc_only_no_reply_suggestion,
    "already_replied_informational": already_replied_informational,
    "priority_ordering": priority_ordering,
    "calm_day_concise": calm_day_concise,
    "briefing_format_valid": briefing_format_valid,
    "delta_unreplied_resurface": delta_unreplied_resurface,
    "normal_email_surfaces": normal_email_surfaces,
    "multi_step_decomposition": multi_step_decomposition,
    "single_step_no_split": single_step_no_split,
    "name_from_header": name_from_header,
    "no_sandbox_leak": no_sandbox_leak,
    "zoom_not_teams": zoom_not_teams,
    "poll_reply_sender_only": poll_reply_sender_only,
    # An assertion defined but not registered here resolves to None in
    # run_evals.py's ASSERTIONS.get() and is silently skipped.
    "scheduling_defaults": scheduling_defaults,
    "draft_revision_uses_update": draft_revision_uses_update,
    "reopened_thread_surfaced": reopened_thread_surfaced,
    "no_dead_scheduling_tools": no_dead_scheduling_tools,
    "scheduling_rules_has_canonical_block": scheduling_rules_has_canonical_block,
    "scheduling_rules_lists_canonical_four": scheduling_rules_lists_canonical_four,
}


def get_assert(output: str, context: dict) -> dict:
    """Promptfoo custom assertion entrypoint.

    Called when using: type: python, value: judge.py
    The specific assertion is selected via context.test.metadata.assertion.
    """
    assertion_name = (context.get("test", {})
                      .get("metadata", {})
                      .get("assertion", ""))
    fn = ASSERTIONS.get(assertion_name)
    if not fn:
        return {"pass": False, "score": 0.0,
                "reason": f"Unknown assertion: {assertion_name}"}
    return fn(output, context)
