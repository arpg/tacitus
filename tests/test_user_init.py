"""scripts/install/user_init.py --chat: the Slack path writes no ea-config.json, the Zulip
path writes a chat block that ea_config.get_chat reads back."""
import builtins
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "install"))
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_config  # noqa: E402
import user_init  # noqa: E402

ANSWERS = {
    "Full name": "Test User",
    "Primary work email": "test@example.edu",
    "Graph auth method": "4",
    "Set up Google Calendar now": "n",
    "Run writing style analysis": "n",
}


def _run(monkeypatch, ea_dir, *extra):
    def fake_input(p=""):
        for key, val in ANSWERS.items():
            if key in p:
                return val
        return ""
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(user_init, "detect_system_timezone",
                        lambda: "America/Denver")
    monkeypatch.setattr(sys, "argv",
                        ["scripts/install/user_init.py", "--ea-dir", str(ea_dir), *extra])
    user_init.main()


ZULIP_ARGS = ("--chat", "zulip", "--zulip-site", "http://zulip.test:8080/",
              "--zulip-bot-email", "ea-bot@zulip.test",
              "--zulip-owner-id", "8")


def test_slack_default_writes_no_chat_config(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path)
    assert (tmp_path / "config" / "user-profile.json").exists()
    assert not (tmp_path / "config" / "ea-config.json").exists()


def test_zulip_writes_chat_block(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, *ZULIP_ARGS)
    path = tmp_path / "config" / "ea-config.json"
    json.loads(path.read_text())
    chat = ea_config.get_chat(path)
    assert chat["frontend"] == "zulip"
    z = chat["zulip"]
    assert z["site"] == "http://zulip.test:8080"
    assert z["bot_email"] == "ea-bot@zulip.test"
    assert z["owner_user_id"] == "8"
    assert (z["stream"], z["sched_stream"], z["dm_stream"]) == (
        "ea", "ea-scheduling", "ea-dm")
    assert z["notify"] == ea_config.DEFAULTS["chat"]["zulip"]["notify"]
    out = capsys.readouterr().out
    assert "--- Zulip ---" in out
    assert "Slack Bot Token" not in out


def test_zulip_keeps_other_ea_config_keys(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"profile": "email"}))
    _run(monkeypatch, tmp_path, "--phase", "state", *ZULIP_ARGS)
    data = json.loads((cfg / "ea-config.json").read_text())
    assert data["profile"] == "email"
    assert data["chat"]["frontend"] == "zulip"


def test_preferences_phase_records_each_answer_as_a_rule(monkeypatch, tmp_path, capsys):
    """`--phase preferences` asks the whole first-week bank; a non-empty
    answer becomes a rule in the scratch preferences store and an empty
    one leaves the question for later."""
    import onboarding
    import preferences as prefs
    bank = onboarding.load_bank()
    answers = {f"[{bank[0]['id']}]": "My chair and my students.",
               f"[{bank[2]['id']}]": "A deadline inside two days."}

    def fake_input(p=""):
        for key, val in answers.items():
            if key in p:
                return val
        return ""
    monkeypatch.setattr(builtins, "input", fake_input)
    # The stores are written through resident_io, which treats a tree
    # outside EA_DIR as the owner's and hands the write to the resident.
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv",
                        ["scripts/install/user_init.py", "--ea-dir", str(tmp_path), "--phase", "preferences"])
    user_init.main()

    store = prefs.load(tmp_path / "config" / "preferences.json")
    assert [r["id"] for r in store["rules"]] == [f"onboarding-{bank[0]['id']}",
                                                  f"onboarding-{bank[2]['id']}"]
    assert store["rules"][0]["quote"] == "My chair and my students."
    assert store["rules"][0]["source"] == "onboarding"
    st = onboarding.status(ea_dir=tmp_path)
    assert st["answered"] == [bank[0]["id"], bank[2]["id"]]
    assert len(st["remaining"]) == len(bank) - 2
    out = capsys.readouterr().out
    assert bank[1]["question"] in out and "Recorded 2 rule(s)" in out


def test_full_setup_offers_the_preferences_phase_and_skips_it_by_default(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "Later: python3 scripts/install/user_init.py --phase preferences" in out
    assert not (tmp_path / "state" / "onboarding.json").exists()
    assert not (tmp_path / "config" / "preferences.json").exists()


def test_zulip_requires_server_args(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, "--chat", "zulip",
             "--zulip-site", "http://zulip.test")
    assert not (tmp_path / "config" / "ea-config.json").exists()


# ── --answers: every prompt from a file, never the terminal ──────────

ANSWERS_FILE = ROOT / "tests" / "install" / "answers-email.json"


def _run_answers(monkeypatch, ea_dir, answers_file=ANSWERS_FILE, *extra):
    def no_input(p=""):
        raise AssertionError(f"input() was called with --answers: {p!r}")
    monkeypatch.setattr(builtins, "input", no_input)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(ea_dir / "private"))
    monkeypatch.setattr(sys, "argv", ["scripts/install/user_init.py", "--ea-dir", str(ea_dir),
                                      "--answers", str(answers_file), *extra])
    user_init.main()


def test_answers_file_drives_the_whole_wizard_without_input(monkeypatch, tmp_path, capsys):
    _run_answers(monkeypatch, tmp_path)
    cfg = tmp_path / "config"
    profile = json.loads((cfg / "user-profile.json").read_text())
    assert profile["name"] == "Pat Example" and profile["email"] == "pat.example@example.edu"
    inst = profile["institution"]
    assert inst["domains"] == ["example.edu"] and inst["safety_rail_domain"] == "example.edu"
    assert inst["related_domains"] == ["med.example.edu"]
    assert inst["video"] == {"platform": "Zoom", "link": "https://example.zoom.us/my/pat.example"}
    assert profile["zoom_link"] == inst["video"]["link"]
    assert inst["letterhead"]["template_dir"] == "~/.ea/templates/letterhead"
    sched = json.loads((cfg / "scheduling-preferences.json").read_text())
    assert sched["timezone"] == "America/Chicago", "the answer wins over the detected zone"
    assert sched["slot_selection"]["preferred_duration_minutes"] == 30
    assert sched["calendars"]["google_primary"]["email"] == "pat.example.cal@gmail.com"
    assert sched["response_style"]["student"] == "Warm, brief."
    assert json.loads((cfg / "autonomy-tiers.json").read_text())["task_tiers"]["draft_email_response"]["tier"] == "ask-first"
    assert json.loads((cfg / "group-senders.json").read_text()) == {}
    assert json.loads((cfg / "deadline-senders.json").read_text()) == {"nsf.gov": "grants"}
    assert json.loads((cfg / "preprocessor-config.json").read_text())["stale_thread_days"] == 3
    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines())
    assert env == {"GRAPH_AUTH_METHOD": "device_code",
                   "GRAPH_CLIENT_ID": "00000000-1111-2222-3333-444444444444"}
    assert "dean@example.edu" in (cfg / "vip_senders.txt").read_text()
    assert not (cfg / "reimbursement-config.json").exists()
    push = tmp_path / "private" / "push.json"
    assert json.loads(push.read_text()) == {"ntfy_url": "https://ntfy.sh",
                                            "topic": "ea-install-smoke-7f3a9c2e5b1d4860",
                                            "include_text": False}
    assert push.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state" / "obligations-ledger.json").exists()
    # every key in the file was asked for
    keys = {k for k in json.loads(ANSWERS_FILE.read_text()) if not k.startswith("_")}
    assert keys <= user_init.ANSWERS_USED, keys - user_init.ANSWERS_USED
    assert "answers never asked for" not in capsys.readouterr().out


def test_answers_with_phase_runs_that_phase_only(monkeypatch, tmp_path):
    _run_answers(monkeypatch, tmp_path, ANSWERS_FILE, "--phase", "identity")
    assert sorted(p.name for p in (tmp_path / "config").iterdir()) == ["user-profile.json"]
    assert not (tmp_path / "private").exists()
    _run_answers(monkeypatch, tmp_path, ANSWERS_FILE, "--phase", "push")
    assert (tmp_path / "private" / "push.json").exists()
    # a second identity run keeps the file's other keys and the block
    (tmp_path / "config" / "user-profile.json").write_text(json.dumps(
        {"name": "Old", "email": "old@example.edu", "gmail_accounts": ["x@gmail.com"]}))
    _run_answers(monkeypatch, tmp_path, ANSWERS_FILE, "--phase", "identity")
    profile = json.loads((tmp_path / "config" / "user-profile.json").read_text())
    assert profile["name"] == "Pat Example" and profile["gmail_accounts"] == ["x@gmail.com"]


def test_answers_absent_keys_take_defaults_and_a_missing_required_one_stops(monkeypatch, tmp_path):
    small = tmp_path / "small.json"
    small.write_text(json.dumps({"full-name": "A B", "work-email": "ab@dept.example.org"}))
    _run_answers(monkeypatch, tmp_path, small, "--phase", "identity")
    profile = json.loads((tmp_path / "config" / "user-profile.json").read_text())
    assert profile["slack_channel_name"] == "ea-assistant"
    assert profile["institution"]["domains"] == ["dept.example.org"]
    assert profile["institution"]["safety_rail_domain"] == "dept.example.org"
    assert profile["institution"]["video"]["platform"] == "Zoom"
    # a fresh directory: no existing profile to supply the name as a default
    fresh = tmp_path / "fresh"
    small.write_text(json.dumps({"work-email": "ab@dept.example.org"}))
    with pytest.raises(SystemExit, match="full-name"):
        _run_answers(monkeypatch, fresh, small, "--phase", "identity")
    assert not (fresh / "config" / "user-profile.json").exists()
    small.write_text("[1, 2]")
    with pytest.raises(SystemExit):
        _run_answers(monkeypatch, fresh, small, "--phase", "identity")


def test_tone_out_writes_the_baseline_to_the_file_not_the_config_dir(monkeypatch, tmp_path):
    """The container's owner holds the mail token but not ea-bot's config:
    --out hands the baseline to a file ea-bot then writes."""
    import types
    monkeypatch.setattr(user_init, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(user_init, "prompt_yn", lambda *a, **k: True)
    monkeypatch.setattr(user_init, "analyze_writing_style_deep",
                        lambda sent: {"default_greeting": "Hi {name},", "sample_size": len(sent)})
    monkeypatch.setitem(sys.modules, "graph_client",
                        types.SimpleNamespace(get_sent=lambda days_back: [{}, {}]))
    out = tmp_path / "baseline.json"
    user_init.phase_tone(out)
    assert json.loads(out.read_text())["sample_size"] == 2
    assert (out.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "config").exists(), "--out writes nothing to the config dir"
    user_init.phase_tone()
    assert (tmp_path / "config" / "writing-style-baseline.json").exists()


def test_out_goes_only_with_the_tone_phase(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["scripts/install/user_init.py", "--ea-dir", str(tmp_path),
                                      "--phase", "vip", "--out", str(tmp_path / "x.json")])
    with pytest.raises(SystemExit):
        user_init.main()


def test_scripted_prompts_accept_json_shapes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda p="": pytest.fail("input() called"))
    monkeypatch.setattr(user_init, "ANSWERS", {
        "yes-text": "Yes", "no-text": "n", "yes-bool": True, "empty": "",
        "list": ["a@x.org", " b@x.org ", ""], "csv": "c@x.org, d@x.org", "number": 45,
        "time": "08:30", "bad-time": "soon",
    })
    monkeypatch.setattr(user_init, "ANSWERS_USED", set())
    yn = user_init.prompt_yn
    assert yn("Q", default=False, key="yes-text") is True
    assert yn("Q", default=True, key="no-text") is False
    assert yn("Q", default=False, key="yes-bool") is True
    assert yn("Q", default=True, key="empty") is True, "an empty answer is Enter"
    assert yn("Q", default=False, key="absent") is False
    assert user_init.prompt_list("Q", key="list") == ["a@x.org", "b@x.org"]
    assert user_init.prompt_list("Q", key="csv") == ["c@x.org", "d@x.org"]
    assert user_init.prompt_list("Q", key="absent") == []
    assert user_init.prompt("Q", "30", key="number") == "45"
    assert user_init.prompt("Q", "30", key="empty") == "30"
    assert user_init.prompt("Q", key="absent") == ""
    assert user_init.prompt_time("Q", "09:00", key="time") == "08:30"
    with pytest.raises(SystemExit, match="bad-time"):
        user_init.prompt_time("Q", "09:00", key="bad-time")
    assert user_init.answer_key("Tenant ID (blank = multi-tenant 'common')") == "tenant-id-blank-multi-tenant-common"
    assert {"yes-text", "absent", "bad-time"} <= user_init.ANSWERS_USED


def test_auth_secrets_go_to_the_owner_private_file(monkeypatch, tmp_path):
    """The client secret and the chat tokens land in ~/.ea-private/secrets.env
    (0600 in a 0700 directory), never in ~/.ea, which is ea-bot space."""
    answers = tmp_path / "answers-auth.json"
    answers.write_text(json.dumps({
        "graph-auth-method": "confidential", "graph-client-id": "cid",
        "graph-tenant-id": "tid", "graph-client-secret": "s3cret",
        "graph-sign-in-now": False, "google-calendar-now": False,
        "slack-bot-token": "bot-token-fake", "slack-app-token": "app-token-fake",
        "slack-channel-id": "C1", "slack-owner-id": "U1"}))
    ea = tmp_path / "ea"
    _run_answers(monkeypatch, ea, answers, "--phase", "auth")
    secrets = ea / "private" / "secrets.env"
    text = secrets.read_text()
    assert "GRAPH_CLIENT_SECRET=s3cret" in text and "SLACK_BOT_TOKEN=bot-token-fake" in text
    assert secrets.stat().st_mode & 0o777 == 0o600
    assert (ea / "private").stat().st_mode & 0o777 == 0o700
    env = (ea / ".env").read_text()
    assert "GRAPH_CLIENT_ID=cid" in env and "EA_SLACK_OWNER_ID=U1" in env
    assert "s3cret" not in env and "token-fake" not in env
    assert not (ea / ".env.secrets").exists()


def test_no_terminal_and_no_answers_stops_with_the_way_out(monkeypatch, tmp_path):
    def eof(p=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", eof)
    monkeypatch.setattr(sys, "argv", ["scripts/install/user_init.py", "--ea-dir", str(tmp_path),
                                      "--phase", "identity"])
    with pytest.raises(SystemExit) as e:
        user_init.main()
    assert "--answers FILE" in str(e.value)


def test_template_placeholders_are_never_offered_as_defaults(monkeypatch, tmp_path):
    """scripts/install/setup.sh copies the templates before the identity phase runs (on the
    host and in the container), so the template's placeholders are in the
    file; the prompts offer none of them, and the template keys the phase
    does not ask about survive."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("user-profile.json", "scheduling-preferences.json"):
        (cfg / name).write_text((ROOT / "config" / name).read_text())
    shown = []

    def fake_input(p=""):
        shown.append(p)
        return {"Full name": "Pat Example", "Primary work email": "pat@uni.example"}.get(
            p.split(" [")[0].rstrip(": "), "")
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(sys, "argv", ["scripts/install/user_init.py", "--ea-dir", str(tmp_path), "--phase", "identity"])
    user_init.main()
    text = "\n".join(shown)
    for placeholder in ("Your Name", "you@example.edu", "DARPA", "Example University",
                        "ai-assistant-yourname"):
        assert placeholder not in text, placeholder
    saved = json.loads((cfg / "user-profile.json").read_text())
    assert saved["name"] == "Pat Example" and saved["institution"]["domains"] == ["uni.example"]
    assert "gmail_accounts_note" in saved and "family_note" in saved
    sched = json.loads((cfg / "scheduling-preferences.json").read_text())
    monkeypatch.setattr(sys, "argv", ["scripts/install/user_init.py", "--ea-dir", str(tmp_path), "--phase", "scheduling"])
    monkeypatch.setattr(user_init, "detect_system_timezone", lambda: "America/Denver")
    user_init.main()
    after = json.loads((cfg / "scheduling-preferences.json").read_text())
    assert "ea_managed_calendar" in sched and after["ea_managed_calendar"] == sched["ea_managed_calendar"]
