#!/usr/bin/env python3
"""Prompt-assembly smoke tests for run-agent.sh.

Bash performs backtick command substitution inside double quotes, so a
backtick in the double-quoted PROMPT string kills every agent at the
assignment under `set -e`, before Claude is invoked and before anything
posts to Slack. The eval suite judges model OUTPUT, so it passes while
the harness cannot assemble a prompt at all.

These tests evaluate the REAL prompt assignments extracted from
run-agent.sh through bash under the script's own `set -euo pipefail`,
once per agent skill file, and assert two things:

  1. the assignment evaluates (a substitution that DIES is caught), and
  2. every literal line of the string survives into the value verbatim
     (a substitution that silently SUCCEEDS, e.g. `date`, is caught by
     the mutated line even though the exit code stays 0).

A static no-unescaped-backticks check runs alongside, and a meta-test
re-injects a recorded offending line to prove this file flags it.

Note for prompt editors: the evaluation stubs every variable the prompt
interpolates. If you add a new ${VAR} to the prompt, `set -u` fails
these tests with "VAR: unbound variable" — add a stub to _stub_env().
Skill-file content is interpolated as ${SKILL_CONTENT}, and variable
expansion does not re-evaluate its value, so backticks in skills/*.md
are safe and deliberately untested here.

Usage:
    python3 tests/test_run_agent_prompt_assembly.py
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_AGENT = REPO / "run-agent.sh"
SKILL_DIR = REPO / "skills"

PROMPT_OPEN = 'PROMPT="You are running as the EA agent'
PROMPT_CLOSE = '\nBegin now."'
RETRY_OPEN = 'RETRY_PROMPT="$PROMPT'
RETRY_CLOSE = ':coffee: for afternoon."'


def _extract(open_anchor: str, close_anchor: str) -> str:
    """Pull one multi-line string assignment out of run-agent.sh.

    Fails loudly rather than silently skipping: if an anchor moves, the
    person who moved it must point this harness at the new text, or the
    exact bug class this file exists for goes back to being invisible.
    """
    src = RUN_AGENT.read_text()
    assert src.count(open_anchor) == 1, (
        f"expected exactly 1 occurrence of {open_anchor!r} in {RUN_AGENT}, "
        f"found {src.count(open_anchor)} — update the anchors in "
        f"{Path(__file__).name}")
    start = src.index(open_anchor)
    end = src.index(close_anchor, start)
    return src[start:end + len(close_anchor)]


def _skill_files() -> list[Path]:
    """Every skill run-agent.sh can feed into the prompt, read from the
    script itself so a new agent is covered without editing this file."""
    names = re.findall(r'SKILL_FILE="\$SKILL_DIR/([A-Za-z0-9._-]+)"',
                       RUN_AGENT.read_text())
    assert names, "no SKILL_FILE assignments found in run-agent.sh"
    paths = [SKILL_DIR / n for n in sorted(set(names))]
    missing = [p.name for p in paths if not p.exists()]
    assert not missing, f"skill files named in run-agent.sh but absent: {missing}"
    return paths


def _stub_env(skill_content: str, agent: str = "morning-briefing",
              warning: str = "") -> dict[str, str]:
    """Every variable the PROMPT assignment interpolates."""
    return {
        "AGENT": agent,
        "PREPROCESS_WARNING": warning,
        "SKILL_CONTENT": skill_content,
        "BRIEFING_FILE": "/home/ea-bot/.ea/state/briefing-latest.json",
        # The prompt points the agent at its own preprocess output; for the
        # full modes OUTPUT_FILE == BRIEFING_FILE, list-digest has its own.
        "OUTPUT_FILE": "/home/ea-bot/.ea/state/briefing-latest.json",
        "EA_BOT_SCRIPT_DIR": "/home/ea-bot/ea-system",
        "EA_BOT_HOME": "/home/ea-bot",
    }


def _evaluate_raw(assignment: str, env: dict[str, str],
                  varname: str = "PROMPT") -> subprocess.CompletedProcess:
    """Run the assignment through bash exactly as run-agent.sh would:
    same shell, same set flags, then print the resulting value."""
    defs = "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())
    script = ("set -euo pipefail\n" + defs + assignment +
              f'\nprintf "%s" "${{{varname}}}"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60)


def _evaluate(assignment: str, env: dict[str, str],
              varname: str = "PROMPT") -> str:
    r = _evaluate_raw(assignment, env, varname)
    assert r.returncode == 0, (
        f"prompt assignment did not evaluate (rc={r.returncode}) — this is "
        f"the 534e9da failure shape; every cron agent dies before Claude "
        f"is invoked. bash said: {r.stderr.strip()[:400]}")
    return r.stdout


def _string_body(assignment: str) -> str:
    """The text between the opening and closing double quote."""
    return assignment.split('"', 1)[1].rsplit('"', 1)[0]


def _unescape_dq(line: str) -> str:
    """What bash keeps of a backslash escape inside double quotes."""
    return re.sub(r'\\([\\$"`])', r'\1', line)


def _literal_lines(assignment: str) -> list[str]:
    """Source lines with no intentional expansion: these must reach the
    evaluated value verbatim. Lines without alphanumerics (blank, the
    lone `}` closing a ${VAR:+...} expansion) carry no signal — skip."""
    out = []
    for ln in _string_body(assignment).splitlines():
        if "$" in ln or not re.search(r"[A-Za-z0-9]", ln):
            continue
        out.append(_unescape_dq(ln))
    return out


def _unescaped_backticks(assignment: str) -> list[tuple[int, str]]:
    """(line_no, line) for every backtick bash would execute."""
    offenders = []
    for i, ln in enumerate(_string_body(assignment).splitlines(), 1):
        if "`" in re.sub(r"\\.", "", ln):
            offenders.append((i, ln.strip()))
    return offenders


# ── the assembly itself, per agent ──────────────────────────────────

def test_prompt_assignment_evaluates_per_agent():
    assignment = _extract(PROMPT_OPEN, PROMPT_CLOSE)
    for skill in _skill_files():
        agent = skill.stem
        content = skill.read_text()
        out = _evaluate(assignment, _stub_env(content, agent=agent))
        assert out.startswith(f"You are running as the EA agent '{agent}'"), \
            f"{agent}: prompt does not open with the agent line: {out[:120]!r}"
        assert out.rstrip().endswith("Begin now."), \
            f"{agent}: prompt lost its closing line"
        assert content.strip() in out, \
            f"{agent}: skill content did not survive into the prompt"


def test_every_literal_line_survives_evaluation():
    """A backtick pair around a command that EXISTS substitutes silently:
    rc stays 0 and the text is quietly mangled. Catch it by requiring
    every expansion-free source line verbatim in the evaluated value."""
    assignment = _extract(PROMPT_OPEN, PROMPT_CLOSE)
    literal = _literal_lines(assignment)
    assert len(literal) >= 10, (
        f"only {len(literal)} literal lines found — the extraction or the "
        f"skip heuristic is broken, this test would be checking nothing")
    out = _evaluate(assignment, _stub_env("skill body placeholder"))
    missing = [l for l in literal if l not in out]
    assert not missing, (
        "literal prompt lines were mutated during evaluation (an unescaped "
        f"substitution ate them): {missing[:3]}")


def test_preprocess_warning_alternate_expansion():
    """The ${PREPROCESS_WARNING:+...} block is its own quoting hazard."""
    assignment = _extract(PROMPT_OPEN, PROMPT_CLOSE)
    flag = "WARNING: calendar fetch failed; availability is INCOMPLETE."
    with_warning = _evaluate(assignment, _stub_env("s", warning=flag))
    assert flag in with_warning, "a set PREPROCESS_WARNING never made it in"
    without = _evaluate(assignment, _stub_env("s", warning=""))
    assert flag not in without


def test_retry_prompt_assignment_evaluates():
    assignment = _extract(RETRY_OPEN, RETRY_CLOSE)
    base = "BASE PROMPT SENTINEL"
    out = _evaluate(assignment,
                    {"PROMPT": base, "VALIDATION": "missing :sunny: anchor"},
                    varname="RETRY_PROMPT")
    assert out.startswith(base), "retry prompt dropped the base prompt"
    assert "missing :sunny: anchor" in out
    for line in _literal_lines(assignment):
        assert line in out, f"retry prompt mutated a literal line: {line!r}"


# ── the static guard ────────────────────────────────────────────────

def test_no_unescaped_backticks_in_prompt_strings():
    for open_a, close_a in ((PROMPT_OPEN, PROMPT_CLOSE),
                            (RETRY_OPEN, RETRY_CLOSE)):
        offenders = _unescaped_backticks(_extract(open_a, close_a))
        assert not offenders, (
            "unescaped backtick(s) inside a double-quoted prompt string — "
            "bash executes these as command substitution (534e9da killed "
            f"every full agent run this way): {offenders}")


# ── proof this file catches a recorded offending line ───────────────

def test_the_2026_08_31_incident_is_caught():
    """Re-inject a recorded offending line and require both guards to
    fire. If the substitution ever exits 0, the literal-line check still
    has to flag the mutation."""
    good = _extract(PROMPT_OPEN, PROMPT_CLOSE)
    incident = ("id-less rows score 0% closure recall at every revision "
                "ever shipped; the same rows with ids score 100%. `add` "
                "derives the conversation id from the message id for you.")
    bad = good.replace(PROMPT_CLOSE, "\n" + incident + PROMPT_CLOSE)
    assert incident in bad  # the injection itself worked

    offenders = _unescaped_backticks(bad)
    assert offenders, "static backtick guard missed the 534e9da line"

    r = _evaluate_raw(bad, _stub_env("s"))
    dynamic_caught = (r.returncode != 0
                      or incident not in r.stdout)
    assert dynamic_caught, (
        "dynamic evaluation neither failed nor mutated the injected line — "
        "the harness would NOT have caught the 2026-08-31 incident")


# ── coverage stays complete as the script grows ─────────────────────

def test_literal_command_rule_evaluates():
    """The fifth rule is its own multi-line string; it must survive
    set -euo pipefail and keep its literal $(...), the script path and
    the mail verbs the gate picks."""
    assignment = _extract('LITERAL_RULE="5. EVERY Bash CALL',
                          'as if the shell were gone.\n"')
    env = {**_stub_env(""), "MAIL_VERBS": "search, get-message"}
    out = _evaluate(assignment, env, varname="LITERAL_RULE")
    assert "no \\$(...)" not in out and "no $(...)" in out
    assert "/home/ea-bot/ea-system/preprocessor/<script>.py" in out
    assert "(search, get-message)." in out
    missing = [l for l in _literal_lines(assignment) if l not in out]
    assert not missing, f"literal rule lines were mutated: {missing[:3]}"


def test_all_multiline_strings_are_covered():
    """A NEW multi-line double-quoted assignment in run-agent.sh is a new
    place for this bug class to live. Fail until it is either covered
    here or consciously allow-listed."""
    known = {
        "PROMPT",          # covered above
        "RETRY_PROMPT",    # covered above
        "LITERAL_RULE",    # covered above (on unless EA_LITERAL_COMMANDS=0)
        # the python -c body for strip_preamble; its behaviour is pinned
        # end-to-end by test_run_agent_preamble.py
        "STRIP_PREAMBLE_MAX_LINES",
    }
    found = set()
    for ln in RUN_AGENT.read_text().splitlines():
        m = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\+?="', ln)
        if m and len(re.findall(r'(?<!\\)"', ln)) % 2 == 1:
            found.add(m.group(1))
    assert {"PROMPT", "RETRY_PROMPT"} <= found, (
        f"expected to find the PROMPT and RETRY_PROMPT assignments, got "
        f"{sorted(found)} — the anchors in {Path(__file__).name} have "
        f"drifted from run-agent.sh")
    new = found - known
    assert not new, (
        f"new multi-line double-quoted string(s) in run-agent.sh not "
        f"covered by the prompt-assembly harness: {sorted(new)} — add an "
        f"evaluation test here (or allow-list with a pointer to whatever "
        f"does cover it)")


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
