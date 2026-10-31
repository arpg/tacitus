#!/usr/bin/env python3
"""The chat lane's prompts name the harness's real denial text and the
command shape the allowlist accepts.

The lanes run `claude -p --permission-mode dontAsk`, so a Bash call whose
command line matches no allowlist pattern (a `cd` prefix, a relative
script name, two commands joined with `;`) is refused with the CLI's
don't-ask message. A prompt that keys its retry rule on a phrase the CLI
no longer emits leaves the model reading one refusal as a lane-wide
permission failure, and the turn ends with nothing done. These checks keep
the rule on the phrase the CLI prints and in every lane's tools block, not
only the scheduling branch.

Usage:
    python3 tests/test_lane_denial_rule.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT = REPO / "ea-chat-bot.py"

DENIAL = ("Permission to use Bash has been denied because Claude Code is "
          "running in don't ask mode")


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def _flat(text: str) -> str:
    """The prompt wraps at 72 columns; compare with one space per break."""
    return " ".join(text.split())


def test_tools_block_carries_the_shape_rule_for_every_lane():
    text = BOT.read_text()
    block = _section(text, 'return f"""Tools available:', "\ndef resolve_bot_user_id")
    head = block[: block.index("- Read/Write/Edit files")]
    assert "COMMAND SHAPE" in head, "the shape rule leads the tools block"
    assert DENIAL in _flat(head), "the rule names the CLI's own denial text"
    assert "never a lane outage" in head
    # The old scheduling-only copy is gone: one rule, every lane.
    assert "ABSOLUTE PATHS ONLY" not in block


def test_slim_reply_prompt_retries_on_the_real_denial_text():
    text = BOT.read_text()
    paths = _section(text, "PATHS:\nAll ea-system scripts", "EFFICIENCY RULE")
    assert DENIAL in _flat(paths)
    assert "requires approval" not in paths, "the CLI no longer prints this phrase"
    assert "run it again" in _flat(paths)


def test_installed_cli_still_prints_that_denial():
    """When the CLI is on this host, its bundle contains the phrase the
    prompts key on; a CLI update that rewords it fails here first."""
    exe = shutil.which("claude")
    if not exe:
        print("  (skipped: no claude on PATH)")
        return
    real = Path(exe).resolve()
    candidates = [real] + sorted(real.parent.glob("*"))
    needle = DENIAL.replace("Bash", "${e}").encode()
    for path in candidates:
        if path.is_file():
            try:
                if needle in path.read_bytes():
                    return
            except OSError:
                continue
    raise AssertionError(f"{real}: the CLI no longer prints {DENIAL!r}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


def test_tools_block_renders_with_every_placeholder_stubbed():
    """The block is an f-string: a literal brace in its prose (a JSON example)
    becomes a format spec and raises on every chat turn, which py_compile
    does not catch. Render it with each placeholder stubbed."""
    import ast
    text = BOT.read_text()
    start = text.index('return f"""Tools available:') + len("return ")
    end = text.index('"""', start + 4) + 3
    tree = ast.parse(text[start:end], mode="eval")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    out = eval(compile(tree, "<tools-block>", "eval"), {n: "x" for n in names})
    assert out.startswith("Tools available")
