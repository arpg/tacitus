"""The online-meeting rule follows the configured video platform: the
overlay names it as the one to use, and no prompt or check hard-codes
another platform as forbidden."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import install_overlay  # noqa: E402
import remote_sched_prompt  # noqa: E402


def _profile(tmp_path, monkeypatch, video):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    (cfg / "user-profile.json").write_text(json.dumps({
        "name": "Avery Example", "email": "avery@example.edu",
        "institution": {"name": "Example University", "domains": ["example.edu"],
                        "video": video}}))


@pytest.mark.parametrize("platform", ["Zoom", "Microsoft Teams", "Google Meet"])
def test_overlay_names_the_configured_platform(tmp_path, monkeypatch, platform):
    _profile(tmp_path, monkeypatch, {"platform": platform, "link": "https://meet.example/avery"})
    [line] = [ln for ln in install_overlay._institution_section() if "Online meetings" in ln]
    assert f"Online meetings use {platform}." in line
    assert "another platform" in line
    assert "Never create a Teams meeting" not in line


def test_remote_scheduling_prompt_names_the_configured_platform(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"platform": "Microsoft Teams", "link": ""})
    text = remote_sched_prompt.fill(remote_sched_prompt.REMOTE_FILE.read_text())
    assert "uses Microsoft Teams and no other platform" in text
    assert "never\n   Teams" not in text and "Do not create Teams meetings" not in text


@pytest.mark.parametrize("name", ["CLAUDE.md", "prompts/CLAUDE-scheduling.md", "prompts/CLAUDE-scheduling-remote.md"])
def test_core_prompts_do_not_forbid_a_named_platform(name):
    text = (REPO / name).read_text()
    assert "Teams meeting" not in text
    assert "never Teams" not in text and "never\n   Teams" not in text
