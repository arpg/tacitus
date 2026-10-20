#!/usr/bin/env python3
"""scripts/ea-doctor.py: the install checklist as code, read-only.

Runs it in --ci mode (templates only) so the wall proves the checklist
itself runs anywhere, and drives the live checks against a scratch
EA_DIR to prove a bare directory is reported as the misses it has, with
a fix on each, and that nothing is written.

Usage:
    python3 tests/test_ea_doctor.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCTOR = REPO / "scripts" / "ea-doctor.py"


def _run(*args, ea_dir=None, private_dir=None, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    if ea_dir:
        env["EA_DIR"] = str(ea_dir)
    # never the host's real private dir
    env["EA_PRIVATE_DIR"] = str(private_dir or tempfile.mkdtemp(prefix="ea-doctor-private-"))
    return subprocess.run([sys.executable, str(DOCTOR), *args], capture_output=True,
                          text=True, env=env, timeout=120)


def test_ci_mode_checks_only_the_repo_and_passes():
    r = _run("--ci", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    names = {x["name"] for x in doc["results"]}
    assert "template: config/user-profile.json" in names
    assert "template: config/ea-channels.json" in names
    assert not any(n.startswith(("service:", "slack:", "env:", "ea-bot")) for n in names), names
    assert doc["fail"] == 0


def test_every_required_template_is_in_the_repo_and_documented():
    sys.path.insert(0, str(REPO / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    setup = (REPO / "SETUP.md").read_text()
    for name in mod.REQUIRED_CONFIG:
        assert (REPO / "config" / name).exists(), name
        assert name in setup, f"SETUP.md never mentions {name}"
    for scope in mod.REQUIRED_SLACK_SCOPES:
        assert f"`{scope}`" in setup, f"SETUP.md does not list the Slack scope {scope}"
    for key in mod.REQUIRED_ENV + mod.REQUIRED_SECRETS:
        assert key in setup, f"SETUP.md does not mention {key}"


def test_a_bare_ea_dir_reports_misses_with_fixes_and_writes_nothing():
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d = Path(td)
        before = sorted(p.name for p in d.iterdir())
        r = _run("--json", ea_dir=d)
        assert r.returncode == 1, "a bare directory must FAIL"
        doc = json.loads(r.stdout)
        fails = {x["name"]: x for x in doc["results"] if x["status"] == "FAIL"}
        for name in ("config: user-profile.json", "~/.ea/.env", "secret: SLACK_BOT_TOKEN",
                     "auth: msal_token_cache.json"):
            assert name in fails, (name, sorted(fails))
            assert fails[name]["fix"], f"{name} has no fix line"
        assert sorted(p.name for p in d.iterdir()) == before, "the doctor wrote into EA_DIR"


def test_private_dir_stores_and_webhooks_are_checked():
    """The private dir must be 0700 and the owner's; a click-time store
    left in ~/.ea/state is a warning; the webhook file must be in the
    private dir, and a copy left in ~/.ea/config is a warning."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d, priv = Path(td) / "ea", Path(td) / "private"
        (d / "state").mkdir(parents=True)
        (d / "config").mkdir()
        priv.mkdir(mode=0o700)
        (d / "state" / "button-payloads.json").write_text("{}")
        (d / "config" / "slack_webhooks.json").write_text("{}")
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        res = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        assert res["private dir"]["status"] == "PASS", res["private dir"]
        assert res["private store: button-payloads.json"]["status"] == "WARN"
        assert res["private store: draft-confirmations.json"]["status"] == "PASS"
        assert res["webhooks: slack_webhooks.json"]["status"] == "FAIL"
        assert "sudo -u ea-bot rm" in res["webhooks: slack_webhooks.json"]["fix"]
        # moved: the config check reads the private copy; a leftover warns
        (priv / "slack_webhooks.json").write_text("{}")
        os.chmod(priv, 0o755)
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        res = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        assert res["webhooks: slack_webhooks.json"]["status"] == "WARN"
        assert res["private dir"]["status"] == "FAIL", "0755 is not crh-only"
        (d / "config" / "slack_webhooks.json").unlink()
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        res = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        assert res["webhooks: slack_webhooks.json"]["status"] == "PASS"
        assert res["config: slack_webhooks.json"]["status"] == "PASS"


def test_a_zulip_install_is_not_asked_for_anything_slack():
    """With chat.frontend zulip the doctor names no Slack env, secret,
    scheduling channel or webhook file, required or optional."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d, priv = Path(td) / "ea", Path(td) / "private"
        (d / "config").mkdir(parents=True)
        priv.mkdir(mode=0o700)
        (d / "config" / "ea-config.json").write_text(json.dumps(
            {"profile": "assistant", "chat": {"frontend": "zulip"}}))
        (d / ".env").write_text("EA_MODEL=x\n")
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        names = [x["name"] for x in json.loads(r.stdout)["results"]]
        # the bot's unit keeps its name on every frontend
        slack = [n for n in names if ("SLACK" in n.upper() or "EA_SCHED_CHANNEL_ID" in n)
                 and n != "service: ea-chat-bot"]
        assert slack == [], slack
        assert "secret: ZULIP_API_KEY" in names


def test_the_secrets_check_names_the_owner_private_file():
    """The secrets check and every secret fix name ~/.ea-private/secrets.env;
    none offers the legacy file in ea-bot's tree."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d, priv = Path(td) / "ea", Path(td) / "private"
        d.mkdir()
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        res = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        assert "~/.ea/.env.secrets" not in res
        assert res["~/.ea-private/secrets.env"]["status"] == "FAIL"
        for x in res.values():
            assert ".env.secrets" not in x["fix"], x
        assert "~/.ea-private/secrets.env" in res["secret: SLACK_BOT_TOKEN"]["fix"]
        priv.mkdir(mode=0o700)
        (priv / "secrets.env").write_text("SLACK_BOT_TOKEN=a\nSLACK_APP_TOKEN=b\n")
        os.chmod(priv / "secrets.env", 0o600)
        r = _run("--json", "--profile", "assistant", ea_dir=d, private_dir=priv)
        res = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        assert res["~/.ea-private/secrets.env"]["status"] == "PASS", res["~/.ea-private/secrets.env"]


def test_the_email_profile_asks_for_no_chat_key():
    """No chat platform: no Slack or Zulip key, required or optional, and
    the ~/.ea/.env fix points at the Graph keys, not the Slack appendix."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d, priv = Path(td) / "ea", Path(td) / "private"
        d.mkdir()
        r = _run("--json", "--profile", "email", ea_dir=d, private_dir=priv)
        results = json.loads(r.stdout)["results"]
        names = [x["name"] for x in results]
        chat = [n for n in names if any(k in n.upper() for k in ("SLACK", "ZULIP", "EA_SCHED"))
                and not n.startswith(("config", "service"))]
        assert chat == [], chat
        env = next(x for x in results if x["name"] == "~/.ea/.env")
        assert env["status"] == "WARN" and "GRAPH_" in env["fix"] and "Slack" not in env["fix"]


def test_text_output_prints_a_fix_under_each_miss():
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        r = _run(ea_dir=Path(td))
        assert "[ FAIL ] config: user-profile.json" in r.stdout
        assert "fix: cp" in r.stdout
        assert r.stdout.strip().splitlines()[-1].endswith(f"({td})")


def test_expect_missing_exits_zero_only_when_every_fail_is_named():
    """--expect-missing names checks, or prefixes of their names, whose
    FAIL is expected; the exit code counts the others. A name that
    matches no FAIL is reported and changes nothing."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d = Path(td)
        fails = [x["name"] for x in json.loads(_run("--json", ea_dir=d).stdout)["results"]
                 if x["status"] == "FAIL"]
        assert len(fails) > 2, fails
        # every FAIL named, plus one that does not occur: exit 0
        r = _run("--json", "--expect-missing", ",".join(fails + ["EA-Managed calendar"]), ea_dir=d)
        assert r.returncode == 0, r.stdout[-800:]
        doc = json.loads(r.stdout)
        assert doc["fail"] == len(fails), "the FAILs are still reported"
        assert doc["unexpected_fail"] == []
        assert doc["expected_missing_unmatched"] == ["EA-Managed calendar"]
        # one left out: exit 1, and the JSON names it
        r = _run("--json", "--expect-missing", ",".join(fails[1:]), ea_dir=d)
        assert r.returncode == 1
        assert json.loads(r.stdout)["unexpected_fail"] == [fails[0]]
        # a prefix covers a family: "config:" stands for every config FAIL
        config_fails = [n for n in fails if n.startswith("config:")]
        assert config_fails
        others = [n for n in fails if not n.startswith("config:")]
        r = _run("--json", "--expect-missing", ",".join(["config:"] + others), ea_dir=d)
        assert r.returncode == 0, json.loads(r.stdout)["unexpected_fail"]
        # the text output says what was expected and what was not
        r = _run("--expect-missing", fails[0], ea_dir=d)
        assert r.returncode == 1
        assert f"expected missing: {fails[0]}" in r.stdout
        assert "unexpected FAIL: " in r.stdout and fails[1] in r.stdout
        r = _run("--expect-missing", ",".join(fails), ea_dir=d)
        assert r.returncode == 0 and "no unexpected FAIL" in r.stdout
        # without the flag nothing changes
        r = _run("--json", ea_dir=d)
        assert r.returncode == 1 and "unexpected_fail" not in json.loads(r.stdout)


def test_probe_codex_is_ok_or_warn_and_never_a_fail():
    """--probe-codex runs `codex_cli.py probe` when the script exists:
    exit 0 is ok, anything else a WARN, an absent script a WARN. Codex is
    optional, so the probe never adds a FAIL. EA_CODEX_CLI names the
    script, so the test never runs a real codex."""
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        d = Path(td)
        good = d / "good.py"
        good.write_text("import sys; print('codex says OK'); sys.exit(0)\n")
        bad = d / "bad.py"
        bad.write_text("import sys; print('no codex here', file=sys.stderr); sys.exit(1)\n")

        def probe(script: Path) -> dict:
            r = _run("--json", "--probe-codex", ea_dir=d, env_extra={"EA_CODEX_CLI": str(script)})
            return {x["name"]: x for x in json.loads(r.stdout)["results"]}["codex probe"]

        absent = probe(d / "missing.py")
        assert absent["status"] == "WARN" and "nothing to probe" in absent["detail"]
        assert probe(good) == {"status": "PASS", "name": "codex probe", "detail": "codex says OK", "fix": ""}
        failed = probe(bad)
        assert failed["status"] == "WARN" and "rc=1" in failed["detail"] and "no codex here" in failed["detail"]
        # without the flag the probe does not run; in --ci mode neither
        for args in (("--json",), ("--json", "--ci", "--probe-codex")):
            r = _run(*args, ea_dir=d, env_extra={"EA_CODEX_CLI": str(good)})
            assert "codex probe" not in {x["name"] for x in json.loads(r.stdout)["results"]}, args


def _results(priv: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="ea-doctor-") as td:
        r = _run("--json", ea_dir=Path(td), private_dir=priv)
    return {x["name"]: x for x in json.loads(r.stdout)["results"]}


def _private_file(priv: Path, name: str, doc) -> None:
    p = priv / name
    p.write_text(json.dumps(doc))
    p.chmod(0o600)


def test_the_persistence_digest_fails_after_36_hours_without_a_run():
    """No snapshot yet is a WARN (a new install); a recent run passes; a
    run more than 36 hours old FAILs."""
    from datetime import datetime, timedelta, timezone
    priv = Path(tempfile.mkdtemp(prefix="ea-doctor-private-"))
    got = _results(priv)["persistence digest"]
    assert got["status"] == "WARN" and "no run yet" in got["detail"] and got["fix"]
    for hours, status in ((2, "PASS"), (35, "PASS"), (37, "FAIL"), (24 * 9, "FAIL")):
        when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        _private_file(priv, "persistence-digest.json", {"checked": when, "files": {}})
        got = _results(priv)["persistence digest"]
        assert got["status"] == status, (hours, got)
        if status == "FAIL":
            assert "36 h" in got["detail"] and "persistence-digest.py" in got["fix"]
    _private_file(priv, "persistence-digest.json", {"files": {}})
    assert _results(priv)["persistence digest"]["status"] == "FAIL"


def test_the_dashboard_loopback_fallback_is_a_warning():
    sys.path.insert(0, str(REPO / "preprocessor"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor_dash", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import project_dashboard
    assert mod.DASH_FALLBACK_FILE == project_dashboard.FALLBACK_FILE
    priv = Path(tempfile.mkdtemp(prefix="ea-doctor-private-"))
    assert "dashboard: loopback fallback" not in _results(priv)
    _private_file(priv, mod.DASH_FALLBACK_FILE,
                  {"wanted": "100.101.102.103", "bound": "127.0.0.1", "since": "2030-01-01T00:00:00Z"})
    got = _results(priv)["dashboard: loopback fallback"]
    assert got["status"] == "WARN" and "100.101.102.103" in got["detail"]
    assert "EA_DASH_TOKEN" in got["fix"] and "set-secret.sh" in got["fix"]


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


def test_share_masks_identities_and_keeps_the_report_shape():
    """--share is what a user pastes into a public issue: addresses, the
    account in a home path, URLs, hosts and long ids become placeholders;
    check names, statuses and ea-bot's own paths stay."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = ("[  ok  ] zulip: get_profile  (bot ea-bot@zulip.example id 9)\n"
            "[ FAIL ] profile: name and email  (pat.lee@example.edu)\n"
            "           fix: edit /home/patlee/.ea/config/user-profile.json\n"
            "[  ok  ] deploy tree  (/home/ea-bot/ea-system at 903f647)\n"
            "[ warn ] chat site  (https://eps.tail1234.ts.net:8443, host eps.tail1234.ts.net)\n"
            "[  ok  ] calendar  (id 0123456789abcdef0123456789abcdef)\n")
    masked = mod.share_mask(text)
    for gone in ("pat.lee@example.edu", "ea-bot@zulip.example", "/home/patlee", "eps.tail1234",
                 "0123456789abcdef0123456789abcdef", "https://"):
        assert gone not in masked, gone
    for kept in ("[ FAIL ] profile: name and email", "/home/ea-bot/ea-system", "903f647",
                 "/home/<user>/.ea/config/user-profile.json", "<email>", "<host>", "<id>", "<url>"):
        assert kept in masked, kept
    r = subprocess.run([sys.executable, str(DOCTOR), "--ci", "--share"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.rstrip().endswith("(--share: addresses, account names, hosts and ids masked)")

