"""The EA's instructions and the security doc say what the code does.

  * CLAUDE.md does not tell the EA that ~/.ea/config is read-only: ea-bot
    owns it, and the persistence digest reports each change;
  * SECURITY.md describes the OAuth token files as the code accepts them:
    0600, or 0660 in the owner's own group, which the owner checks accept
    only while that group holds no other user; the owner writes them with
    token_file_mode(), never group write;
  * SECURITY.md names the scoped file-tool grants and the persistence
    digest, which exist.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = (ROOT / "CLAUDE.md").read_text()
SEC = (ROOT / "docs" / "SECURITY.md").read_text()


def test_claude_md_does_not_call_the_config_read_only():
    assert "is readable, not writable" not in CLAUDE
    assert "`~/.ea/config/` is writable" in CLAUDE


def test_token_modes_match_the_code():
    graph = (ROOT / "preprocessor" / "graph_client.py").read_text()
    assert "The owner writes token files with token_file_mode(), never" in graph
    assert "mode, gid = _owner_trust.token_file_mode()" in graph
    assert "mode=0o660" not in graph
    # No sentence calls every OAuth token owner-only 0600.
    assert not re.search(r"auth/[^\n]*\* is owner-only `0600`", SEC)
    assert "are owner-only `0600`, so the `ea-bot` user" not in SEC
    assert SEC.count("`0660` in") >= 3


def test_the_new_controls_are_named_and_exist():
    for path in ("preprocessor/resident_write_paths.py", "scripts/persistence-digest.py"):
        assert path in SEC
        assert (ROOT / path).exists()
