#!/usr/bin/env python3
"""Setup asks for the mode first, with three names and one plain line each.

The /setup skill, the question the terminal and the setup page ask, and
the config module's own summaries name exactly reviewer, assistant and
collaborator, in that order, with the same one-line descriptions; no
setup text offers the names the modes replaced.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "scripts" / "install"))

import ea_config  # noqa: E402
import setup_questions as Q  # noqa: E402

SKILL = (ROOT / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
MODES = ("reviewer", "assistant", "collaborator")


def _section_one() -> str:
    return SKILL[SKILL.index("## 1. Mode"):SKILL.index("## 2. Identity")]


def test_the_config_module_and_the_question_name_the_same_three_modes():
    assert ea_config.PROFILES == MODES
    (q,) = [q for q in Q.questions("profile", "web") if q["key"] == "profile"]
    assert [row[0] for row in q["choices"]] == list(MODES)
    for mode, label in ((row[0], row[1]) for row in q["choices"]):
        assert "\n" not in label and label == f"{mode}: {Q.MODE_LINES[mode]}", mode
    assert Q.MODE_LINES == ea_config.PROFILE_SUMMARY


def test_the_skill_offers_exactly_the_three_modes_with_their_lines():
    offered = re.findall(r"^- `([a-z-]+)`: (.+)$", _section_one(), re.M)
    assert [name for name, _ in offered] == list(MODES)
    for name, line in offered:
        assert line == ea_config.PROFILE_SUMMARY[name], name
    assert "recommend `assistant`" in _section_one()


def test_each_line_is_short_and_plain():
    """A person on a phone reads each line once: one sentence or two, short
    words, no jargon from the code."""
    for mode, line in ea_config.PROFILE_SUMMARY.items():
        assert len(line.split()) <= 25 and line.endswith("."), mode
        for word in ("profile", "feature", "resident", "plugin", "cron", "Slack", "Zulip"):
            assert word not in line, (mode, word)
        assert chr(0x2014) not in line


def test_no_setup_text_offers_the_replaced_names():
    texts = {"SKILL.md": SKILL,
             "setup_questions.py": (ROOT / "scripts/install/setup_questions.py").read_text(),
             "setup_web.py": (ROOT / "scripts/install/setup_web.py").read_text(),
             "docs/setup-web.md": (ROOT / "docs/setup-web.md").read_text(),
             "docs/install-testing.md": (ROOT / "docs/install-testing.md").read_text()}
    for name, text in texts.items():
        assert "review-only" not in text, name
        assert not re.search(r"`email` (profile|mode)|(profile|mode) `?email`?\b|email profile",
                             text), name


if __name__ == "__main__":
    import traceback
    failed = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            try:
                v()
                print(f"PASS {k}")
            except Exception:
                failed += 1
                traceback.print_exc()
                print(f"FAIL {k}")
    sys.exit(1 if failed else 0)
