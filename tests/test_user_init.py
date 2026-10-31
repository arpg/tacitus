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


def test_slack_writes_no_chat_config(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, "--chat", "slack")
    assert (tmp_path / "config" / "user-profile.json").exists()
    assert not (tmp_path / "config" / "ea-config.json").exists()


WEB_HOST, WEB_LOGIN = "host.tail0000.ts.net", "owner@example.test"
WEB_URL = f"https://{WEB_HOST}:8444/chat"


def _web_of(tmp_path) -> dict:
    return ea_config.get_chat(tmp_path / "config" / "ea-config.json")


def test_the_web_chat_is_the_default_choice_and_writes_its_block(monkeypatch, tmp_path, capsys):
    """A fresh assistant run offers the web chat first: Enter takes it, and
    the chat block gets the web block's port and base path; the phases
    after it ask no Slack token."""
    _run(monkeypatch, tmp_path)
    data = json.loads((tmp_path / "config" / "ea-config.json").read_text())
    assert "quiet_hours" in data, "the template's keys come along"
    chat = _web_of(tmp_path)
    assert (chat["frontend"], chat["shadow"]) == ("web", "")
    assert (chat["web"]["port"], chat["web"]["base_path"]) == (8470, "/chat")
    assert chat["web"]["public_url"] == "" and chat["web"]["tailscale_login"] == ""
    out = capsys.readouterr().out
    assert "Phase 7b: Chat Platform" in out and "--- Web chat ---" in out
    assert "tailscale serve --bg --https=8444 --set-path /chat http://127.0.0.1:8470/chat" in out
    assert "Slack Bot Token" not in out


def test_chat_web_flags_write_the_address_and_the_login(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, "--chat", "web", "--web-host", WEB_HOST + ".",
         "--web-login", WEB_LOGIN)
    web = _web_of(tmp_path)["web"]
    assert (web["public_url"], web["tailscale_login"], web["port"], web["base_path"]) == \
        (WEB_URL, WEB_LOGIN, 8470, "/chat")
    assert "Slack Bot Token" not in capsys.readouterr().out
    for bad in (("--web-host", "https://host.example"), ("--web-login", "no-at-sign")):
        with pytest.raises(SystemExit):
            _run(monkeypatch, tmp_path / "bad", "--chat", "web", *bad)
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path / "bad", "--chat", "zulip", "--web-host", WEB_HOST)
    assert not (tmp_path / "bad").exists()


def test_the_chat_phase_from_an_answers_file(monkeypatch, tmp_path, capsys):
    """/setup writes answers-chat.json and runs the chat phase: the web
    block is written into the chat block ea-config.json has, keeping its
    other keys, and the auth phase then asks no Slack token. Choosing
    Zulip or Slack later sets the frontend and keeps the rest."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"profile": "assistant", "quiet_hours": {},
                                                    "chat": {"frontend": "slack",
                                                             "zulip": {"stream": "ea"}}}))
    answers = tmp_path / "answers-chat.json"
    answers.write_text(json.dumps({"chat-platform": "web", "web-chat-host": WEB_HOST,
                                   "web-chat-login": WEB_LOGIN}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "chat")
    data = json.loads((cfg / "ea-config.json").read_text())
    assert data["profile"] == "assistant" and "quiet_hours" in data
    assert data["chat"]["zulip"] == {"stream": "ea"}
    assert data["chat"]["web"] == {"port": 8470, "base_path": "/chat", "public_url": WEB_URL,
                                   "tailscale_login": WEB_LOGIN}
    auth = tmp_path / "answers-auth.json"
    auth.write_text(json.dumps({"graph-auth-method": "skip", "google-calendar-now": False}))
    _run_answers(monkeypatch, tmp_path, auth, "--phase", "auth")
    assert "slack-bot-token" not in user_init.ANSWERS_USED
    assert "--- Web chat ---" in capsys.readouterr().out
    # the next run offers what is there
    ctx = user_init.context()
    assert (ctx["chat_choice"], ctx["web_host"], ctx["web_login"]) == ("web", WEB_HOST, WEB_LOGIN)
    answers.write_text(json.dumps({"chat-platform": "zulip"}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "chat")
    chat = json.loads((cfg / "ea-config.json").read_text())["chat"]
    assert chat["frontend"] == "zulip" and chat["web"]["public_url"] == WEB_URL
    # an email install is asked nothing and writes nothing
    data = json.loads((cfg / "ea-config.json").read_text())
    (cfg / "ea-config.json").write_text(json.dumps({**data, "profile": "email"}))
    before = (cfg / "ea-config.json").read_bytes()
    answers.write_text(json.dumps({"chat-platform": "slack"}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "chat")
    assert (cfg / "ea-config.json").read_bytes() == before


def test_a_bad_web_host_names_its_key(monkeypatch, tmp_path):
    answers = tmp_path / "answers-chat.json"
    answers.write_text(json.dumps({"chat-platform": "web", "web-chat-host": "host:8444"}))
    with pytest.raises(SystemExit, match="web-chat-host"):
        _run_answers(monkeypatch, tmp_path, answers, "--phase", "chat")


def test_a_slack_install_is_offered_slack_again(monkeypatch, tmp_path):
    """The template's chat block says Slack with no choice behind it; the
    Slack ids in ~/.ea/.env are the choice, and the default follows them."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"chat": {"frontend": "slack"}}))
    user_init.use_ea_dir(tmp_path)
    assert user_init.context()["chat_choice"] is None
    (tmp_path / ".env").write_text("EA_SLACK_CHANNEL_ID=C0EXAMPLE\n")
    assert user_init.context()["chat_choice"] == "slack"
    assert user_init.context("zulip")["chat_choice"] == "zulip"


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


def test_the_profile_phase_records_the_profile_and_the_notice_once(monkeypatch, tmp_path, capsys):
    """`--phase profile` asks what /setup and the setup page ask first, and
    records both the way setup.sh --acknowledged does, starting from the
    template when ea-config.json does not exist yet."""
    answers = tmp_path / "answers-profile.json"
    answers.write_text(json.dumps({"profile": "review-only", "acknowledged": True}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "profile")
    cfg = json.loads((tmp_path / "config" / "ea-config.json").read_text())
    assert cfg["profile"] == "review-only" and cfg["acknowledged"]["responsibility"]
    assert "quiet_hours" in cfg, "the template's keys come along"
    assert "Tacitus is not an ordinary program." in capsys.readouterr().out
    # accepted once: a second run neither asks again nor moves the date
    cfg["acknowledged"]["responsibility"] = "2026-01-02"
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps(cfg))
    answers.write_text(json.dumps({"profile": "email"}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "profile")
    cfg = json.loads((tmp_path / "config" / "ea-config.json").read_text())
    assert cfg["profile"] == "email" and cfg["acknowledged"]["responsibility"] == "2026-01-02"
    # a no stops the install
    fresh = tmp_path / "fresh"
    answers.write_text(json.dumps({"profile": "email", "acknowledged": False}))
    with pytest.raises(SystemExit, match="accept the notice"):
        _run_answers(monkeypatch, fresh, answers, "--phase", "profile")
    assert not (fresh / "config" / "ea-config.json").exists()


def test_the_chat_platform_defaults_to_the_chat_block(monkeypatch, tmp_path, capsys):
    """With a Zulip chat block in ea-config.json and no --chat, the auth
    phase asks no Slack token."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"profile": "assistant",
                                                    "chat": {"frontend": "zulip"}}))
    answers = tmp_path / "answers-auth.json"
    answers.write_text(json.dumps({"graph-auth-method": "skip", "google-calendar-now": False}))
    _run_answers(monkeypatch, tmp_path, answers, "--phase", "auth")
    assert "slack-bot-token" not in user_init.ANSWERS_USED
    assert "--- Zulip ---" in capsys.readouterr().out


def test_a_bad_scripted_answer_names_its_key(monkeypatch, tmp_path):
    answers = tmp_path / "answers.json"
    for phase, bad in (("identity", {"full-name": "A B", "work-email": "not-an-address"}),
                       ("scheduling", {"default-duration-minutes": "half an hour"}),
                       ("autonomy", {"conservative-defaults": False,
                                     "tier-create-calendar-hold": "sometimes"})):
        answers.write_text(json.dumps(bad))
        key = [k for k in bad if k not in ("full-name", "conservative-defaults")][0]
        with pytest.raises(SystemExit, match=key):
            _run_answers(monkeypatch, tmp_path / phase, answers, "--phase", phase)


def test_the_terminal_google_sign_in_calls_the_client_setup(tmp_path, monkeypatch, capsys):
    """google_sign_in calls the function the client module has; a name
    that does not exist there made every terminal sign-in print "auth
    failed" and move on."""
    import sys as _sys, types as _types
    import importlib
    ui = importlib.import_module("user_init") if "user_init" in _sys.modules else None
    if ui is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("user_init", str(ROOT / "scripts" / "install" / "user_init.py"))
        ui = importlib.util.module_from_spec(spec); spec.loader.exec_module(ui)
    monkeypatch.setattr(ui, "AUTH_DIR", tmp_path)
    (tmp_path / "google_client_secret.json").write_text("{}")
    calls = []
    fake = _types.ModuleType("google_calendar_client"); fake.setup_all_accounts = lambda: calls.append("setup")
    monkeypatch.setitem(_sys.modules, "google_calendar_client", fake)
    ui.google_sign_in()
    out = capsys.readouterr().out
    assert calls == ["setup"] and "successful" in out and "failed" not in out
