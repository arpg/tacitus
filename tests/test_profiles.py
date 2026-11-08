#!/usr/bin/env python3
"""The install's mode (reviewer | assistant | collaborator) is one setting
that every lane reads: the config module defines it and maps the old names,
the launchers add the no-drafting deny list when the drafting switch is
off, the cron lane writes reports to files and posts to the chat only when
the mode has one, scripts/install/setup.sh and the doctor take it as a
flag, and the /setup skill asks for it first."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_config  # noqa: E402

FRAGMENT = ROOT / "scripts" / "disallowed-tools.no-drafting.txt"
LAUNCHERS = ("run-agent.sh", "run-plugin.sh", "scripts/ea-bot-shell.sh",
             "scripts/ea-resident-inner.sh")


def _cfg(profile: str | None, **extra) -> Path:
    d = Path(tempfile.mkdtemp(prefix="ea-test-profile-")) / "config"
    d.mkdir()
    p = d / "ea-config.json"
    doc = {"profile": profile} if profile else {}
    doc.update(extra)
    p.write_text(json.dumps(doc))
    return p


def _mode(profile: str | None, **extra) -> str:
    return ea_config.get_profile(_cfg(profile, **extra))


def test_the_three_modes_and_their_flags():
    assert ea_config.PROFILES == ("reviewer", "assistant", "collaborator")
    assert set(ea_config.PROFILE_FEATURES) == set(ea_config.PROFILES)
    keys = {frozenset(v) for v in ea_config.PROFILE_FEATURES.values()}
    assert len(keys) == 1, "every mode declares the same feature keys"
    reviewer, assistant, collaborator = (ea_config.PROFILE_FEATURES[m] for m in ea_config.PROFILES)
    # reviewer: no chat, no buttons, no resident, no rooms, no plugins; drafts on
    assert reviewer["drafting"] and not any(v for k, v in reviewer.items() if k != "drafting")
    # assistant: one chat with everything but rooms
    assert not assistant["rooms"] and all(v for k, v in assistant.items() if k != "rooms")
    # collaborator: everything
    assert all(collaborator.values())
    assert set(ea_config.PROFILE_SUMMARY) == set(ea_config.PROFILES)
    for line in ea_config.PROFILE_SUMMARY.values():
        assert "\n" not in line and chr(0x2014) not in line


def test_a_fresh_install_defaults_to_assistant_and_unknown_values_too():
    assert ea_config.DEFAULT_PROFILE == "assistant"
    assert _mode(None) == "assistant"
    assert _mode("gmail-first") == "assistant"
    assert ea_config.get_profile(Path("/nonexistent/ea-config.json")) == "assistant"
    template = json.loads((ROOT / "config" / "ea-config.json").read_text())
    assert template["profile"] == "assistant" and template["profile_version"] == 2
    assert ea_config.resolve_profile(template) == ("assistant", {}, False)


def test_old_names_map_to_the_modes_until_renamed():
    assert _mode("email") == "reviewer"
    assert _mode("review-only") == "reviewer"
    # the old assistant had project rooms: without the marker it is collaborator
    assert _mode("assistant") == "collaborator"
    assert _mode("assistant", profile_version=2) == "assistant"
    assert _mode("collaborator") == "collaborator" and _mode("reviewer") == "reviewer"
    assert ea_config.profile_features(config_path=_cfg("assistant"))["rooms"]
    assert not ea_config.profile_features(config_path=_cfg("assistant", profile_version=2))["rooms"]
    # review-only implies drafting off
    assert not ea_config.profile_features(config_path=_cfg("review-only"))["drafting"]
    assert ea_config.profile_features(config_path=_cfg("email"))["drafting"]
    # a typed name: a mode is itself, an old name is its mode
    assert ea_config.profile_name("assistant") == ("assistant", {})
    assert ea_config.profile_name("email") == ("reviewer", {})
    assert ea_config.profile_name("review-only") == ("reviewer", {"drafting": False})


def test_a_read_of_an_old_name_logs_one_line(caplog):
    ea_config._LEGACY_LOGGED.clear()
    path = _cfg("email")
    with caplog.at_level("WARNING", logger="ea_config"):
        ea_config.get_profile(path)
        ea_config.get_profile(path)
        ea_config.profile_features(config_path=path)
    lines = [r for r in caplog.records if r.name == "ea_config"]
    assert len(lines) == 1 and "normalize-profile" in lines[0].getMessage()


def test_normalize_profile_rewrites_only_after_a_yes(capsys):
    path = _cfg("review-only", keep="me")
    assert ea_config.normalize_profile(path, ask=lambda q: False) == 3
    assert json.loads(path.read_text()) == {"profile": "review-only", "keep": "me"}
    assert ea_config.normalize_profile(path, ask=lambda q: True) == 0
    assert json.loads(path.read_text()) == {"profile": "reviewer", "keep": "me",
                                            "profile_version": 2, "drafting": False}
    assert ea_config.get_profile(path) == "reviewer"
    assert not ea_config.profile_features(config_path=path)["drafting"]
    # a mode already: nothing to rename
    assert ea_config.normalize_profile(path, yes=True) == 0
    old = _cfg("assistant")
    assert ea_config.normalize_profile(old, yes=True) == 0
    assert json.loads(old.read_text()) == {"profile": "collaborator", "profile_version": 2}
    assert "It already works as `collaborator`" in capsys.readouterr().out


def test_set_profile_records_the_mode_with_its_marker(capsys):
    path = _cfg("assistant")  # the old name: collaborator
    assert ea_config.set_profile("assistant", path) == 0
    doc = json.loads(path.read_text())
    assert doc == {"profile": "assistant", "profile_version": 2}
    assert not ea_config.profile_features(config_path=path)["rooms"]
    out = capsys.readouterr().out
    assert "setup.sh" not in out, "between the two chat modes nothing else changes"
    assert ea_config.set_profile("reviewer", path) == 0
    assert "setup.sh --profile reviewer --install-cron" in capsys.readouterr().out
    assert ea_config.set_profile("gmail-first", path) == 2


def test_the_drafting_switch_works_in_every_mode():
    for mode in ea_config.PROFILES:
        on = _cfg(mode, profile_version=2)
        off = _cfg(mode, profile_version=2, drafting=False)
        assert ea_config.profile_features(config_path=on)["drafting"], mode
        assert not ea_config.profile_features(config_path=off)["drafting"], mode
        # a named mode takes the install's switch too
        assert not ea_config.profile_features(mode, config_path=off)["drafting"], mode
    # only a literal false turns it off
    assert ea_config.profile_features(config_path=_cfg("reviewer", drafting="false"))["drafting"]


def test_the_report_schedule():
    assert ea_config.get_report_schedule(_cfg("reviewer")) == "weekdays"
    for val in ("weekdays", "daily", "none"):
        assert ea_config.get_report_schedule(_cfg("reviewer", reports={"schedule": val})) == val
    assert ea_config.get_report_schedule(_cfg("reviewer", reports={"schedule": "hourly"})) == "weekdays"


def test_config_cli_answers_for_shell_callers():
    env = {**os.environ, "EA_DIR": str(_cfg("review-only").parent.parent)}
    cli = [sys.executable, str(ROOT / "preprocessor" / "ea_config.py")]

    def run(*args):
        return subprocess.run(cli + list(args), capture_output=True, text=True, env=env)
    assert run("profile").stdout.strip() == "reviewer"
    assert run("feature", "slack").returncode == 1
    assert run("feature", "chat").returncode == 1
    assert run("feature", "drafting").returncode == 1
    assert json.loads(run("features", "--profile", "collaborator").stdout)["rooms"] is True
    assert json.loads(run("features", "--profile", "email").stdout)["slack"] is False
    env["EA_DIR"] = str(_cfg(None).parent.parent)
    assert run("feature", "slack").returncode == 0
    assert run("feature", "rooms").returncode == 1, "a fresh install is the assistant: no rooms"
    assert run("reports").stdout.strip() == "weekdays"
    env["EA_DIR"] = str(_cfg("assistant").parent.parent)
    assert run("feature", "rooms").returncode == 0, "the old assistant keeps its rooms"
    r = run("normalize-profile")
    assert r.returncode == 3 and "collaborator" in r.stdout and "--yes" in r.stdout


def test_no_drafting_denies_every_drafting_verb_in_every_launcher():
    entries = [ln.strip() for ln in FRAGMENT.read_text().splitlines()
               if ln.strip() and not ln.startswith("#")]
    # Every mail verb that writes a draft body, from the classification
    # test_send_content_binding keeps: those that make a new draft id, and
    # create-reply and update-draft. A new one fails here until it is denied.
    sys.path.insert(0, str(ROOT / "tests"))
    from test_send_content_binding import NOT_DRAFT_CHANGING, MM
    for tool, why in NOT_DRAFT_CHANGING.items():
        verbs = {v for v, r in why.items() if r.startswith("makes a new draft id")}
        verbs |= {"create-reply", "update-draft"} & set(MM.DRAFT_EDIT_VERBS.get(tool, {}))
        for verb in sorted(verbs):
            for form in (f"Bash(python3 *{tool}.py {verb} *)", f"Bash(*{tool}.py {verb} *)"):
                assert form in entries, form
    assert "mcp__claude_ai_Gmail__create_draft" in entries
    # keyed on the drafting switch, not on a mode name
    for launcher in LAUNCHERS:
        src = (ROOT / launcher).read_text()
        assert "disallowed-tools.no-drafting.txt" in src, launcher
        assert 'ea_config.py" feature drafting' in src, launcher
        assert "review-only" not in src, launcher
    bot = (ROOT / "ea-chat-bot.py").read_text()
    fn = bot[bot.index("def _load_disallowed_tools("):bot.index("_DISALLOWED_TOOLS = _load_disallowed_tools()")]
    assert '"disallowed-tools.no-drafting.txt"' in fn and 'profile_features()["drafting"]' in fn
    assert not (ROOT / "scripts" / "disallowed-tools.review-only.txt").exists()


def test_the_launchers_add_the_fragment_exactly_when_drafting_is_off():
    """The shell check each launcher runs: `ea_config.py feature drafting`
    fails with drafting off (and on an unreadable answer), so the fragment
    is added."""
    cli = [sys.executable, str(ROOT / "preprocessor" / "ea_config.py"), "feature", "drafting"]
    for doc, want_denied in (({"profile": "reviewer", "profile_version": 2}, False),
                             ({"profile": "reviewer", "profile_version": 2, "drafting": False}, True),
                             ({"profile": "collaborator", "profile_version": 2, "drafting": False}, True),
                             ({"profile": "review-only"}, True),
                             ({"profile": "email"}, False)):
        path = _cfg(None)
        path.write_text(json.dumps(doc))
        env = {**os.environ, "EA_DIR": str(path.parent.parent)}
        denied = subprocess.run(cli, env=env, capture_output=True).returncode != 0
        assert denied == want_denied, doc


def test_cron_lane_writes_the_report_and_posts_only_with_a_chat():
    src = (ROOT / "run-agent.sh").read_text()
    assert 'ea_config.py" profile' in src and 'ea_config.py" feature chat' in src
    assert 'REPORT_DIR="$STATE_DIR/reports"' in src
    assert '"$REPORT_DIR/${AGENT}-latest.md"' in src
    assert 'if [[ "$HAS_SLACK" == 1 ]]; then' in src
    # the report file is written before the chat decision
    assert src.index('${AGENT}-latest.md') < src.index("slack.post_to_slack('ea-assistant', msg)")
    # with drafting off the no-chat block says so instead of promising drafts
    assert 'if [[ "${HAS_DRAFTING:-1}" != 1 ]]; then' in src and "${NO_DRAFTING_LINE}" in src


def test_setup_and_doctor_take_the_mode():
    setup = (ROOT / "scripts/install/setup.sh").read_text()
    assert "--profile)" in setup and "HAS_SLACK=1" in setup and "--reports)" in setup
    assert 'd["profile"] = profile' in setup and 'd["profile_version"] = 2' in setup
    assert 'for path in "$SCRIPT_DIR"/config/*' in setup, "every template is copied"
    assert "review-only|assistant" not in setup
    for bad in (["--profile", "gmail-first"], ["--reports", "hourly"]):
        r = subprocess.run(["bash", str(ROOT / "scripts/install/setup.sh"), *bad],
                           capture_output=True, text=True)
        assert r.returncode == 2 and "unknown" in r.stderr, bad
    doctor = [sys.executable, str(ROOT / "scripts" / "ea-doctor.py")]
    bare = tempfile.mkdtemp(prefix="ea-test-doctor-")
    # a scratch private dir, so the host's real webhook file is not found
    env = dict(os.environ, EA_PRIVATE_DIR=tempfile.mkdtemp(prefix="ea-test-private-"))

    def doc(*args):
        r = subprocess.run(doctor + ["--ea-dir", bare, *args, "--json"],
                           capture_output=True, text=True, env=env)
        return json.loads(r.stdout)
    rev, full = doc("--profile", "reviewer"), doc("--profile", "collaborator")
    r_fail = {r["name"] for r in rev["results"] if r["status"] == "FAIL"}
    f_fail = {r["name"] for r in full["results"] if r["status"] == "FAIL"}
    assert "config: slack_webhooks.json" in f_fail and "config: slack_webhooks.json" not in r_fail
    assert "~/.ea-private/secrets.env" in f_fail and "~/.ea-private/secrets.env" not in r_fail
    assert r_fail <= f_fail, "the reviewer mode never fails a check the full mode passes"
    assert any(r["name"] == "profile" and r["detail"] == "reviewer" for r in rev["results"])
    reports = [r for r in rev["results"] if r["name"] == "reports"]
    assert reports and "./run-agent.sh morning-briefing --now" in reports[0]["detail"]
    assert not any(r["name"] == "reports" for r in full["results"])
    # an old name on the command line still works, with a note
    old = doc("--profile", "email")
    assert any(r["name"] == "profile" and r["detail"] == "reviewer" for r in old["results"])
    assert any(r["name"] == "profile name" and r["status"] == "WARN" for r in old["results"])


def test_setup_keeps_the_rooms_of_an_install_that_still_stores_the_old_assistant(tmp_path):
    """The old assistant had project rooms. Run with --profile assistant from
    habit, setup.sh keeps collaborator and says why until the rename; after
    the rename the same flag means one chat with no rooms. With no
    --profile it records nothing new."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_setup_crontab import _live_tree, _run_setup
    live = _live_tree(tmp_path)
    cfg = tmp_path / "home" / ".ea" / "config" / "ea-config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"profile": "assistant"}))
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="assistant")
    assert "Profile: collaborator" in r.stdout and "normalize-profile" in r.stdout
    assert json.loads(cfg.read_text())["profile"] == "assistant", "no rename without a yes"
    assert ea_config.get_profile(cfg) == "collaborator"
    assert ea_config.normalize_profile(cfg, yes=True) == 0
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="assistant")
    doc = json.loads(cfg.read_text())
    assert (doc["profile"], doc["profile_version"]) == ("assistant", 2)
    assert not ea_config.profile_features(config_path=cfg)["rooms"]
    # a stored name with no drafts keeps its switch when its mode is named
    cfg.write_text(json.dumps({"profile": "review-only"}))
    _run_setup(tmp_path, live, answer="n", profile="reviewer")
    doc = json.loads(cfg.read_text())
    assert (doc["profile"], doc["drafting"]) == ("reviewer", False)


def test_the_doctor_offers_the_rename_for_a_stored_old_name():
    ea = _cfg("assistant").parent.parent
    env = dict(os.environ, EA_PRIVATE_DIR=tempfile.mkdtemp(prefix="ea-test-private-"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ea-doctor.py"), "--ea-dir", str(ea),
                        "--json"], capture_output=True, text=True, env=env)
    rows = json.loads(r.stdout)["results"]
    assert any(x["name"] == "profile" and x["detail"] == "collaborator" for x in rows)
    rename = [x for x in rows if x["name"] == "profile name"]
    assert rename and rename[0]["status"] == "WARN"
    assert "normalize-profile" in rename[0]["fix"]
    # nothing was rewritten: the doctor changes nothing
    assert json.loads((ea / "config" / "ea-config.json").read_text()) == {"profile": "assistant"}


def test_setup_skill_exists_and_the_core_points_at_it():
    skill = ROOT / ".claude" / "skills" / "setup" / "SKILL.md"
    text = skill.read_text()
    assert text.startswith("---\nname: setup\n")
    for must in ("reviewer", "assistant", "collaborator", "AskUserQuestion",
                 "ea-doctor.py --profile", "scripts/install/setup.sh --profile", "docs/it-request.md"):
        assert must in text, must
    core = (ROOT / "CLAUDE.md").read_text()
    assert "/setup" in core and ".claude/skills/setup/SKILL.md" in core
    assert "`reviewer`" in core and "`collaborator`" in core and "review-only" not in core
    assert "## Choose a mode" in (ROOT / "docs/SETUP.md").read_text()
    in_repo = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                             capture_output=True).returncode == 0
    if in_repo:  # the Docker image carries the tree without its .git
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(skill)]).returncode
        assert ignored == 1, ".claude/skills must be tracked so the skill deploys"


def test_setup_asks_for_the_institution_and_installs_the_overlay():
    """The skill and docs/SETUP.md both write the institution block, then render
    the overlay as ea-bot and install it in the deployed tree. Neither
    names one institution except as the pointer to the worked example."""
    skill = (ROOT / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    setup = (ROOT / "docs/SETUP.md").read_text()
    for name, text in (("SKILL.md", skill), ("docs/SETUP.md", setup)):
        for must in ("`institution` block", "safety_rail_domain", "video.platform", "video.link",
                     "zoom_link", "sudo -u ea-bot /usr/bin/python3 "
                     "/home/ea-bot/ea-system/preprocessor/install_overlay.py render",
                     "install_overlay.py install --tree /home/ea-bot/ea-system",
                     "config/examples/cu-boulder/"):
            assert must in text, (name, must)
        assert text.index("safety_rail_domain") < text.index("install_overlay.py render"), name
        for para in text.split("\n\n"):
            if re.search(r"\bCU\b|Boulder|\bOIT\b", para):
                assert "config/examples/cu-boulder/" in para, (name, para[:80])
    assert "### The install overlay" in setup

