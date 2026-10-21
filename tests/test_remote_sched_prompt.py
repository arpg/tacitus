"""remote_sched_prompt: the compact prompt's parts, its size against the
lane prompt, and the rebuild from a recorded lane prompt."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import remote_sched_prompt as rsp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.dumps({"name": "Kit", "email": "emerson.vasquez27@example.edu", "role": "Professor", "timezone": "America/Denver",
                      "zoom_link": "https://zoom.example/x", "bio": "long " * 400, "signature": "x" * 500})


def test_compose_is_compact_and_carries_the_parts():
    p = rsp.compose(PROFILE, "## User rules (scopes: scheduling)\n- No Fridays.", "The user posted this NEW message:\n\n> find time with Ana",
                    today="Monday, October 13, 2026")
    assert p.startswith("# Scheduling Assistant (on-prem arm)")
    assert "Today is Monday, October 13, 2026." in p
    assert '"zoom_link": "https://zoom.example/x"' in p and "long long" not in p and "signature" not in p
    assert "- No Fridays." in p and p.rstrip().endswith("> find time with Ana")
    assert "propose-meeting" in p and "pending-event.json" in p and '"ref": "m3"' in p
    # The two modes the first live turn missed: a draft the user wrote is
    # finished with update-draft, and an attendee elsewhere gets a zone.
    assert "update-draft --message-id <m-ref of the draft>" in p and '"own": true' in p and '"is_draft": true' in p
    assert '"timezone":"Europe/London"' in p and "Never create-reply beside a draft the user wrote." in p
    assert len(p) < 12000
    lane = (ROOT / "prompts/CLAUDE-scheduling.md").read_text() + (ROOT / "skills" / "scheduling-rules.md").read_text()
    assert len(p) < len(lane) / 3


def test_rebuild_from_a_recorded_lane_prompt():
    lane = ("# Scheduling Assistant – Operating Instructions\n\nlong\n\n=== USER PROFILE ===\n" + PROFILE
            + "\n=== END USER PROFILE ===\n\nSCHEDULING RULES (authoritative source):\n" + "rule " * 5000
            + "\n\n## User rules (scopes: scheduling)\n- No Fridays.\n\nTools available:\n- Bash\n\n"
            "The user posted this NEW message in #ea-scheduling:\n\n> find time with Ana\n")
    p = rsp.from_lane_prompt(lane)
    assert "rule rule" not in p and '"name": "Kit"' in p and "- No Fridays." in p
    assert p.rstrip().endswith("> find time with Ana") and "Tools available" not in p
    assert len(p) < len(lane) / 3
    # No markers at all: the tail of the prompt is the request.
    p2 = rsp.from_lane_prompt("just a request with no markers")
    assert p2.rstrip().endswith("just a request with no markers")
