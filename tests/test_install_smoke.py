#!/usr/bin/env python3
"""The install smoke (tests/install/) stays runnable and documented.

The smoke itself needs sudo and a fresh home, so it runs on a CI runner
or in a container (tests/install/docker-smoke.sh), not in the wall. What
the wall can check: both scripts parse, the smoke's expected misses are
real check names in the doctor and are the ones docs/install-testing.md
lists, the answers file holds every key the smoke's phases ask for, and
the workflow has the jobs the doc describes, on pinned actions.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import traceback

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "tests" / "install"
SMOKE = INSTALL / "smoke.sh"
DOCKER_SMOKE = INSTALL / "docker-smoke.sh"
ANSWERS = INSTALL / "answers-email.json"
DOC = REPO / "docs" / "install-testing.md"
DOCTOR = REPO / "scripts" / "ea-doctor.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _smoke_expect_missing() -> list[str]:
    m = re.search(r'EXPECT="\$\{EA_SMOKE_EXPECT_MISSING:-([^}]*)\}"', SMOKE.read_text())
    assert m, "smoke.sh sets EXPECT from EA_SMOKE_EXPECT_MISSING with a default"
    return [n.strip() for n in m.group(1).split(",") if n.strip()]


def _doc_expected_names() -> list[str]:
    """The check names the doc's expected-missing table lists, in order."""
    text = DOC.read_text()
    start = text.index("## Expected misses")
    end = text.index("\n## ", start + 1)
    return re.findall(r"^\| `([^`]+)` \|", text[start:end], re.M)


def test_both_scripts_parse_and_are_executable():
    for script in (SMOKE, DOCKER_SMOKE):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"{script.name}: {r.stderr}"
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_expected_misses_are_doctor_check_names_and_are_documented():
    doctor = DOCTOR.read_text()
    names = _smoke_expect_missing()
    assert len(names) == 3, names
    for name in names:
        assert f'"{name}"' in doctor, f"{name!r} is not a check name in ea-doctor.py"
    assert _doc_expected_names() == names, "docs/install-testing.md lists the smoke's names, in order"


def test_answers_file_covers_every_phase_the_smoke_runs():
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "scripts" / "install"))
    import user_init
    phases = re.search(r"^for phase in (.+?); do", SMOKE.read_text(), re.M)
    assert phases, "smoke.sh loops over the config phases"
    listed = phases.group(1).split()
    assert set(listed) <= set(user_init.ALL_PHASES), set(listed) - set(user_init.ALL_PHASES)
    assert "tone" not in listed and "summary" not in listed
    answers = json.loads(ANSWERS.read_text())
    keys = {k for k in answers if not k.startswith("_")}
    for required in ("full-name", "work-email", "institution-domains", "timezone",
                     "graph-auth-method", "graph-client-id", "push-topic"):
        assert required in keys, required
    assert answers["timezone"] == "America/Chicago"
    assert answers["work-email"].endswith("@example.edu")
    assert answers["graph-auth-method"] == "device_code"
    assert answers["push-configure"] is True and answers["graph-sign-in-now"] is False
    # every key names a prompt: the doc's key table is the list
    doc = DOC.read_text()
    for key in sorted(keys):
        assert f"`{key}`" in doc, f"docs/install-testing.md does not list the answer key {key}"


def test_workflow_has_the_tiers_on_pinned_actions():
    if not WORKFLOW.exists():
        # The Docker image carries the tree without .github; the script-style
        # runner counts a pytest skip as a failure, so this is a plain return.
        print("  (no workflow files in this tree; the Docker image leaves .github out)")
        return
    text = WORKFLOW.read_text()
    for job in ("lint-and-validate:", "wall:", "install-smoke-linux:", "install-smoke-macos:"):
        assert job in text, job
    assert "tests/run_wall.py" in text and "tests/secrets_lint.sh" in text
    assert "tests/validate_config.py --ci" in text
    assert "tests/install/smoke.sh" in text
    for line in re.findall(r"uses:\s*(\S+)", text):
        assert re.search(r"@v\d+\.\d+\.\d+$", line), f"{line} is not pinned to a full version"
    assert "macos-latest" in text and "ubuntu-22.04" in text
    assert "workflow_dispatch" in text
    assert "HOME=" in text, "the wall job sets a fake HOME"


def test_setup_sh_has_the_non_interactive_switch():
    setup = (REPO / "scripts/install/setup.sh").read_text()
    assert "--non-interactive" in setup and "EA_NONINTERACTIVE" in setup
    r = subprocess.run(["bash", "-n", str(REPO / "scripts/install/setup.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # the sign-ins and the cron question are the three places it reads the
    # terminal; a run with no terminal on stdin is non-interactive by itself
    assert setup.count('if [[ "$NONINTERACTIVE" == 1 ]]') == 3, "Graph, Google, crontab (elif)"
    assert 'elif [[ "$NONINTERACTIVE" == 1 ]]; then\n    REPLY=n' in setup, "crontab"
    assert '[[ "$NONINTERACTIVE" != 1 && ! -t 0 ]]' in setup
    assert "--install-cron" in setup



def test_the_feedback_form_names_the_skill_sections():
    """The issue form's /setup choices are the skill's own section numbers
    and titles, so a report names a step the skill has."""
    form = REPO / ".github" / "ISSUE_TEMPLATE" / "install-feedback.yml"
    if not form.exists():
        print("  (no issue templates in this tree; the Docker image leaves .github out)")
        return
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    sections = dict(re.findall(r"^## (\d+b?)\. (.+)$", skill, re.M))
    choices = re.findall(r"^        - /setup (.+)$", form.read_text(), re.M)
    assert len(choices) >= 8, choices
    for choice in choices:
        for num, title in re.findall(r"(\d+b?)\. ([A-Z][^(]*?)(?= or \d|\s*\(|$)", choice):
            assert num in sections, f"no section {num} in the skill"
            assert sections[num].startswith(title.strip()), f"{num}: {title!r} vs {sections[num]!r}"
    assert "doctor" in form.read_text() and "made-up values" in form.read_text()


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
