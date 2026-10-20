"""The intake surfaces of a public repository with one maintainer: issue
forms that ask for what triage needs, blank issues off, questions and
ideas routed to Discussions, a security policy that points at private
reporting, a support page, dependency updates once a month, and the stale
workflow that closes only issues waiting on their reporter."""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"


def _absent() -> bool:
    # The Docker image carries the tree without .github; the script-style
    # runner counts a pytest skip as a failure.
    if FORMS.exists():
        return False
    print("  (no .github in this tree; the Docker image leaves it out)")
    return True


def _form(name: str) -> dict:
    return yaml.safe_load((FORMS / name).read_text())


def test_blank_issues_are_off_and_questions_go_to_discussions():
    if _absent():
        return
    cfg = yaml.safe_load((FORMS / "config.yml").read_text())
    assert cfg["blank_issues_enabled"] is False
    urls = [c["url"] for c in cfg["contact_links"]]
    assert any("/discussions/categories/q-a" in u for u in urls)
    assert any("/discussions/categories/ideas" in u for u in urls)
    assert any("/security/advisories/new" in u for u in urls)


def test_the_forms_ask_for_what_triage_needs():
    if _absent():
        return
    bug = _form("bug.yml")
    ids = {b.get("id"): b for b in bug["body"] if b.get("id")}
    for required in ("version", "platform", "profile", "area", "what-happened", "doctor"):
        assert ids[required]["validations"]["required"] is True, required
    assert "ea-doctor.py --share" in ids["doctor"]["attributes"]["description"]
    assert "triage" in bug["labels"] and "bug" in bug["labels"]
    feature = _form("feature.yml")
    fids = {b.get("id") for b in feature["body"]}
    assert {"discussion", "problem", "design", "where", "who"} <= fids
    assert "docs/plugins.md" in feature["body"][0]["attributes"]["value"]
    for name in ("bug.yml", "feature.yml", "install-feedback.yml"):
        text = (FORMS / name).read_text()
        assert "made-up values" in text or "--share" in text, name
        assert chr(0x2014) not in text, name


def test_the_policy_pages_point_at_private_reporting_and_the_doctor():
    sec = (REPO / "SECURITY.md").read_text()
    assert "/security/advisories/new" in sec and "Never open a public issue" in sec
    sup = (REPO / "SUPPORT.md").read_text()
    assert "ea-doctor.py --share" in sup and "docs/plugins.md" in sup
    readme = (REPO / "README.md").read_text()
    assert "## Status and support" in readme and "ea-doctor.py --share" in readme
    contributing = (REPO / "CONTRIBUTING.md").read_text()
    assert "## What gets merged" in contributing and "`needs-info`" in contributing
    for text in (sec, sup):
        assert chr(0x2014) not in text


def test_dependencies_update_monthly_in_groups_and_stale_touches_only_needs_info():
    if _absent():
        return
    dep = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    for u in dep["updates"]:
        assert u["schedule"]["interval"] == "monthly" and u["groups"], u["package-ecosystem"]
    stale = yaml.safe_load((REPO / ".github" / "workflows" / "stale.yml").read_text())
    step = stale["jobs"]["stale"]["steps"][0]
    assert re.match(r"actions/stale@v\d+\.\d+\.\d+$", step["uses"]), step["uses"]
    w = step["with"]
    assert w["only-labels"] == "needs-info" and w["days-before-pr-stale"] == -1
    assert w["days-before-stale"] + w["days-before-close"] == 21
    assert stale["permissions"] == {"issues": "write"}
    pr = (REPO / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    assert "tests/run_wall.py" in pr and "full-ci" in pr and "contributor license" in pr
