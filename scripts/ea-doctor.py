#!/usr/bin/env python3
"""ea-doctor: is this install complete? Read-only. Never changes anything.

The pieces a working install needs are spread across docs/SETUP.md's
appendices, two setup scripts and a crontab file. This script is the
checklist as code: every prerequisite the running system depends on,
checked the way the system checks it, with the fix printed next to each
miss.

    python3 scripts/ea-doctor.py            # the live install (~/.ea, services, Slack)
    python3 scripts/ea-doctor.py --ci       # templates only; no network, no system state
    python3 scripts/ea-doctor.py --json     # machine-readable
    python3 scripts/ea-doctor.py --profile email \
        --expect-missing "ea-bot: Claude login,auth: msal_token_cache.json"
                                            # an install with no accounts yet: exit 0
                                            # when only the named checks FAIL

Exit 1 when any check FAILS (WARNs are advisory). `--ci` never fails on
system state, so it can run in the test wall on any machine.
`--expect-missing` names checks (or prefixes of their names) whose FAIL
is expected; docs/install-testing.md lists the names an install with no
accounts produces. `--probe-claude` and `--probe-codex` each spend one
model call and are opt-in.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
RESULTS: list[dict] = []
# Slack-dependent checks are required only when the profile has Slack.
NEEDS_SLACK = True
# The EA-Managed calendar is required when the profile can create real
# calendar events (the Create button); on the email and review-only
# profiles Google Calendar is optional (docs/SETUP.md "Google OAuth app"), so its checks WARN.
NEEDS_CALENDAR = True
SLACK_CONFIG = ("slack_webhooks.json", "ea-channels.json")

REQUIRED_CONFIG = (
    "user-profile.json", "scheduling-preferences.json", "autonomy-tiers.json",
    "ea-config.json", "slack_webhooks.json", "vip_senders.txt",
    "preferences.json", "list-digests.json", "ea-channels.json",
)
OPTIONAL_CONFIG = ("reimbursement-config.json", "deadline-senders.json",
                   "group-senders.json", "preprocessor-config.json",
                   "style-audiences.json")
REQUIRED_ENV = ("EA_SLACK_CHANNEL_ID", "EA_SLACK_CHANNEL_NAME", "EA_SLACK_OWNER_ID")
OPTIONAL_ENV = ("EA_SCHED_CHANNEL_ID", "EA_MODEL", "EA_RESIDENT_MODEL", "EA_BRIEFING_MODEL",
                "EA_LITERAL_COMMANDS",
                "EA_DASH_TOKEN", "EA_DASH_BIND", "EA_DASH_PORT")
REQUIRED_SECRETS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
# The secrets each chat frontend needs (chat.frontend in ea-config.json).
FRONTEND_SECRETS = {"slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
                    "zulip": ("ZULIP_API_KEY",), "memory": ()}
CHAT_FRONTEND = "slack"
CHAT_CFG: dict = {}
# What the bot actually calls; the app must grant every one of these.
REQUIRED_SLACK_SCOPES = ("chat:write", "channels:history", "channels:read",
                         "groups:history", "groups:read", "groups:write",
                         "im:history", "im:write", "files:read", "files:write",
                         "reactions:write")
SERVICES = (("ea-chat-bot", True), ("ea-dashboard", False), ("ea-mail-mediator", False))
PLACEHOLDERS = {"Your Name", "your.email@example.com", "REPLACE_ME", "CHANGEME", ""}


def rec(status: str, name: str, detail: str = "", fix: str = "") -> None:
    RESULTS.append({"status": status, "name": name, "detail": detail, "fix": fix})


def ok(name, detail="", _fix=""):
    rec("PASS", name, detail)


def warn(name, detail="", fix=""):
    rec("WARN", name, detail, fix)


def fail(name, detail="", fix=""):
    rec("FAIL", name, detail, fix)


# --share: the output a user pastes into a public issue. Addresses, the
# account names in home paths, host names and long ids become fixed
# placeholders, so the shape of the report survives and the identities do
# not. The masking runs on the rendered text, never on the checks.
_SHARE_RULES = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(/(?:home|Users)/)(?!ea-bot/)[^/\s'\"]+"), r"\1<user>"),
    (re.compile(r"https?://[^\s'\"]+"), "<url>"),
    (re.compile(r"\b[a-z0-9-]+\.(?:ts\.net|local|lan|edu|com|org|net)\b"), "<host>"),
    (re.compile(r"\b[0-9a-f]{24,}\b"), "<id>"),
]


def share_mask(text: str) -> str:
    """The text with addresses, home-path user names, URLs, host names and
    long hexadecimal ids replaced by placeholders."""
    for rx, rep in _SHARE_RULES:
        text = rx.sub(rep, text)
    return text


def _read_env(path: Path) -> dict:
    out: dict = {}
    try:
        for ln in path.read_text().splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"\n]*)"?\s*$', ln)
            if m:
                out[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return out


def _json_ok(path: Path) -> tuple[bool, str]:
    try:
        json.loads(path.read_text())
        return True, ""
    except json.JSONDecodeError as e:
        return False, f"invalid JSON: {e}"
    except OSError as e:
        return False, str(e)


# ── checks ────────────────────────────────────────────────────────────

def check_repo():
    for rel, why in (("scripts/disallowed-tools.txt", "the send/create deny list every lane loads"),
                     ("skills/morning-briefing.md", "the briefing skill"),
                     ("config/user-profile.json", "the profile template")):
        p = REPO / rel
        if p.exists():
            ok(f"repo: {rel}")
        else:
            fail(
                f"repo: {rel}", f"missing ({why})",
                "copy it from the repo at github.com/arpg/tacitus or restore from git")
    v = sys.version_info
    if v >= (3, 10):
        ok("python", f"{v.major}.{v.minor}")
    else:
        fail("python", f"{v.major}.{v.minor}", "Python 3.10+ is required (3.12+ on Ubuntu 24.04, see docs/SETUP.md 'Python packages in three places')")


def check_templates():
    cfg = REPO / "config"
    for name in REQUIRED_CONFIG:
        p = cfg / name
        if not p.exists():
            fail(f"template: config/{name}", "missing from the repo", "restore it; scripts/install/setup.sh copies the templates from here (docs/SETUP.md 'Config files')")
            continue
        if name.endswith(".json"):
            good, why = _json_ok(p)
            (ok if good else fail)(f"template: config/{name}", why, "fix the template JSON")
        else:
            ok(f"template: config/{name}")


def _as_ea_bot(*cmd: str) -> subprocess.CompletedProcess | None:
    """Run a read-only probe as ea-bot (sudo -n; the owner may run commands
    as ea-bot). ea-bot's ~/.claude is 0700, so the owner cannot look inside
    it directly. None when sudo would ask for a password."""
    try:
        r = subprocess.run(["sudo", "-n", "-u", "ea-bot", *cmd], capture_output=True,
                           text=True, timeout=20, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 and "password is required" in r.stderr:
        return None
    return r


def _login_state(stdout: str) -> bool | None:
    """`claude auth status` output: logged in or not, by the CLI's own
    word; None when the text is not its JSON with a boolean loggedIn."""
    try:
        d = json.loads(stdout.strip() or "{}")
    except ValueError:
        return None
    v = d.get("loggedIn") if isinstance(d, dict) else None
    return v if isinstance(v, bool) else None


def check_ea_bot_claude(home: Path):
    """ea-bot's own Claude Code (~/.local/bin/claude, docs/SETUP.md 'The service account'), tested as
    ea-bot because the owner cannot always see into ea-bot's home, and
    ea-bot's login: ~/.claude/.credentials.json, or on macOS, where ea-bot
    has no keychain, a CLAUDE_CODE_OAUTH_TOKEN in ~/.claude/settings.json
    (docs/macos-claude-binary.md)."""
    own = home / ".local/bin/claude"
    r = _as_ea_bot("test", "-x", str(own))
    if r is None:
        warn("ea-bot: .local/bin/claude", "cannot test as ea-bot: sudo asks for a password")
    elif r.returncode == 0:
        ok("ea-bot: .local/bin/claude")
    else:
        fail("ea-bot: .local/bin/claude", "missing (the Claude CLI for the ea-bot user)",
             f"sudo -u ea-bot env HOME={home} bash -c 'curl -fsSL https://claude.ai/install.sh | bash' "
             "(ea-bot's own install; docs/SETUP.md 'The service account')")
    probe = ("import json,sys; d=json.load(open(sys.argv[1])); "
             "print('yes' if (d.get('env') or {}).get('CLAUDE_CODE_OAUTH_TOKEN') else 'no')")
    login_fix = (f"cd / && sudo -u ea-bot env HOME={home} {own}, then /login, then Ctrl+C "
                 "(docs/SETUP.md 'Common failures')")
    # The CLI's own answer first: a credentials file can exist with its
    # tokens wiped, which the file test below would still pass.
    if r is not None and r.returncode == 0:
        s = _as_ea_bot("env", f"HOME={home}", str(own), "auth", "status")
        state = _login_state(s.stdout) if s is not None else None
        if state is True:
            ok("ea-bot: Claude login", "claude auth status: logged in")
            return
        if state is False:
            t = _as_ea_bot("/usr/bin/python3", "-c", probe, str(home / ".claude/settings.json"))
            if t is not None and t.stdout.strip() == "yes":
                ok("ea-bot: Claude login", "CLAUDE_CODE_OAUTH_TOKEN in .claude/settings.json (renew yearly)")
            else:
                fail("ea-bot: Claude login",
                     "claude auth status: not logged in (every lane answers 'Not logged in')",
                     login_fix)
            return
    r = _as_ea_bot("test", "-e", str(home / ".claude/.credentials.json"))
    if r is not None and r.returncode == 0:
        ok("ea-bot: Claude login", ".claude/.credentials.json")
        return
    t = _as_ea_bot("/usr/bin/python3", "-c", probe, str(home / ".claude/settings.json"))
    if t is not None and t.stdout.strip() == "yes":
        ok("ea-bot: Claude login", "CLAUDE_CODE_OAUTH_TOKEN in .claude/settings.json (renew yearly)")
    elif r is None or t is None:
        warn("ea-bot: Claude login", "cannot check as ea-bot (sudo asks for a password)")
    else:
        fail("ea-bot: Claude login", "neither .claude/.credentials.json nor a token in .claude/settings.json",
             "Linux: bash scripts/ea-bot-shell.sh and run /login once; macOS: claude setup-token "
             "as ea-bot (docs/macos-claude-binary.md)")


_LOGIN_HINTS = ("login", "log in", "logged in", "api key", "api_key", "anthropic_api_key",
                "authenticat", "credential")
PROBE_PROMPT = "Reply with the word OK."


def check_claude_probe():
    """One Haiku call as ea-bot with the lanes' own flags (claude_cli):
    proves `claude -p` still runs on the subscription login. Opt-in
    (--probe-claude) because it spends a call. Claude Code's docs say
    --bare becomes the default for -p, and a bare run reads no
    subscription login; this is the check that notices."""
    name = "claude -p probe"
    try:
        home = Path(pwd.getpwnam("ea-bot").pw_dir)
    except KeyError:
        fail(name, "no ea-bot user", "sudo bash scripts/install/setup-ea-user.sh")
        return
    own = home / ".local/bin/claude"
    r = _as_ea_bot("test", "-x", str(own))
    if r is None:
        warn(name, "cannot run as ea-bot: sudo asks for a password")
        return
    claude_bin = str(own) if r.returncode == 0 else "claude"
    import claude_cli
    args = claude_cli.apply(
        ["-p", PROBE_PROMPT, "--model", "haiku", "--tools", "", "--strict-mcp-config",
         "--no-session-persistence", "--output-format", "json"],
        claude_bin=claude_bin, as_ea_bot=True)
    flags = args[:args.index("-p")]
    try:
        no_connectors = [f"{k}={v}" for k, v in claude_cli.no_connector_env(args).items()]
        p = subprocess.run(["sudo", "-n", "-u", "ea-bot", "env", f"HOME={home}", *no_connectors,
                            claude_bin, *args],
                           capture_output=True, text=True, timeout=180, cwd="/",
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        fail(name, f"the call did not finish: {e}")
        return
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    try:
        data = json.loads(p.stdout[p.stdout.find("{"):p.stdout.rfind("}") + 1])
    except ValueError:
        data = {}
    answer = str(data.get("result") or "")
    if p.returncode == 0 and not data.get("is_error") and "ok" in answer.lower():
        ok(name, "the subscription login answers"
           + (f" (flags: {' '.join(flags)})" if flags else ""))
        return
    low = out.lower()
    if any(h in low for h in _LOGIN_HINTS):
        fail(name, "claude -p no longer reads the subscription login (the --bare default)",
             "add the CLI's bare-mode opt-out flag to login_flags in the owner's private "
             "claude.json (preprocessor/claude_cli.py), or update the CLI")
    else:
        fail(name, f"rc={p.returncode}: {' '.join(out.split())[:160]}")


CODEX_CLI = "preprocessor/codex_cli.py"


def check_codex_probe():
    """`python3 preprocessor/codex_cli.py probe` (exit 0: codex answers a
    trivial prompt). Opt-in (--probe-codex) because it spends a call.
    Codex is optional, so a missing script or a failed probe is a WARN.
    EA_CODEX_CLI names another script to probe (the tests use it)."""
    name = "codex probe"
    path = Path(os.environ.get("EA_CODEX_CLI") or str(REPO / CODEX_CLI))
    if not path.exists():
        warn(name, f"{path} is not in this tree; nothing to probe")
        return
    try:
        p = subprocess.run([sys.executable, str(path), "probe"], capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL, cwd=str(REPO))
    except (OSError, subprocess.TimeoutExpired) as e:
        warn(name, f"the probe did not finish: {e}")
        return
    out = " ".join(((p.stdout or "") + " " + (p.stderr or "")).split())[:160]
    if p.returncode == 0:
        ok(name, out or "codex answers")
    else:
        warn(name, f"rc={p.returncode}: {out}", "codex is optional; the script's own output says what is missing")


def check_users_and_dirs(ea_dir: Path):
    try:
        pw = pwd.getpwnam("ea-bot")
        ok("ea-bot user", f"uid {pw.pw_uid}, home {pw.pw_dir}")
        home = Path(pw.pw_dir)
        if (home / "ea-system").exists():
            ok("ea-bot: ea-system")
        else:
            fail("ea-bot: ea-system", "missing (the deployed copy every lane runs)",
                 "run scripts/install/setup-ea-user.sh, then scripts/deploy.sh (or commit once: the post-commit hook deploys)")
        check_ea_bot_claude(home)
    except KeyError:
        fail("ea-bot user", "no such user",
             "sudo bash scripts/install/setup-ea-user.sh (creates the user, group ea, sudoers, dirs)")
        return
    me = pwd.getpwuid(os.getuid()).pw_name
    groups = subprocess.run(["id", "-nG", me], capture_output=True, text=True).stdout.split()
    if "ea" in groups:
        ok("owner in group ea", me)
    else:
        fail("owner in group ea", f"{me} is not in group ea",
             f"sudo usermod -aG ea {me}; log out and in")
    for rel, mode_ok, fix in (("config", lambda m: m & 0o050, "chmod 750 ~/.ea/config; chgrp ea"),
                              ("state", lambda m: (m & 0o070) == 0o070 and (m & stat.S_ISGID), "chmod 2775 ~/.ea/state; chgrp ea"),
                              ("logs", lambda m: m & 0o070, "chmod 2775 ~/.ea/logs; chgrp ea"),
                              ("auth", lambda m: m & 0o050, "chmod 750 ~/.ea/auth; chgrp ea")):
        p = ea_dir / rel
        if not p.exists():
            fail(f"dir: ~/.ea/{rel}", "missing", f"mkdir -p {p}; {fix}")
            continue
        m = p.stat().st_mode & 0o7777
        (ok if mode_ok(m) else fail)(f"dir: ~/.ea/{rel}", f"mode {oct(m)}", fix)


def _private_dir() -> Path:
    """The owner-only private dir (EA_PRIVATE_DIR, default
    ~<owner>/.ea-private), read the way the bot reads it."""
    try:
        from chat import private_dir
        return private_dir()
    except Exception:
        return Path(os.environ.get("EA_PRIVATE_DIR") or str(Path.home() / ".ea-private"))


# Stores the bot reads at click time; they live in the private dir, out of
# the LLM user's reach.
PRIVATE_STORES = ("button-payloads.json", "draft-confirmations.json",
                  "bulk-send-batches.json")
WEBHOOKS = "slack_webhooks.json"


def _config_file(cfg: Path, name: str) -> Path:
    """Where a config file is read from: the webhook file from the private
    dir when it is there (as chat_notify reads it), everything else from
    ~/.ea/config."""
    if name == WEBHOOKS:
        private = _private_dir() / name
        if private.exists():
            return private
    return cfg / name


def check_private(ea_dir: Path):
    """The private dir is owner-only; the click-time stores and the webhook
    file live there and not in ea-bot's reach."""
    priv = _private_dir()
    me = pwd.getpwuid(os.getuid()).pw_name
    if not priv.exists():
        fail("private dir", f"{priv} missing",
             f"start the bot once (it creates {priv} 0700), or mkdir -m 700 {priv}")
    else:
        st = priv.stat()
        mode = st.st_mode & 0o777
        owner = pwd.getpwuid(st.st_uid).pw_name
        good = mode == 0o700 and st.st_uid == os.getuid()
        (ok if good else fail)("private dir", f"{priv} mode {oct(mode)} owner {owner}",
                               f"chmod 700 {priv}; chown {me} {priv}")
    state = ea_dir / "state"
    for name in PRIVATE_STORES:
        stale = state / name
        if stale.exists():
            warn(f"private store: {name}", f"a stale copy remains in {state}",
                 f"the bot reads {priv / name}; sudo -u ea-bot rm {stale}")
        else:
            ok(f"private store: {name}", "not in ~/.ea/state")
    if CHAT_FRONTEND == "slack":
        in_priv = (priv / WEBHOOKS).exists()
        in_cfg = (ea_dir / "config" / WEBHOOKS).exists()
        if in_priv and not in_cfg:
            ok(f"webhooks: {WEBHOOKS}", "in the private dir only")
        elif in_priv:
            warn(f"webhooks: {WEBHOOKS}", "a copy remains in ~/.ea/config",
                 f"sudo -u ea-bot rm {ea_dir / 'config' / WEBHOOKS}")
        else:
            fail(f"webhooks: {WEBHOOKS}", "not in the private dir",
                 f"cp {ea_dir / 'config' / WEBHOOKS} {priv / WEBHOOKS}; chmod 600 "
                 f"{priv / WEBHOOKS}; sudo -u ea-bot rm {ea_dir / 'config' / WEBHOOKS}")


def check_config(ea_dir: Path):
    cfg = ea_dir / "config"
    for name in REQUIRED_CONFIG:
        p = _config_file(cfg, name)
        if not p.exists():
            fail(f"config: {name}", "missing", f"cp {REPO}/config/{name} {p}; then edit (docs/SETUP.md 'Config files')")
            continue
        if name.endswith(".json"):
            good, why = _json_ok(p)
            if not good:
                fail(f"config: {name}", why, "fix the JSON")
                continue
        ok(f"config: {name}")
    for name in OPTIONAL_CONFIG:
        p = _config_file(cfg, name)
        (ok if p.exists() else warn)(f"config (optional): {name}",
                                     "" if p.exists() else "absent; defaults apply",
                                     f"cp {REPO}/config/{name} {p} to customise")
    eac = cfg / "ea-config.json"
    if eac.exists():
        try:
            d = json.loads(eac.read_text())
            ack = d.get("acknowledged") if isinstance(d.get("acknowledged"), dict) else {}
            when = str(ack.get("responsibility") or "")
            (ok if when else warn)("responsibility notice",
                                   f"acknowledged {when}" if when else "not acknowledged",
                                   "read 'Your responsibility' in README.md, then run "
                                   "./scripts/install/setup.sh --profile <name> --acknowledged")
        except Exception:
            pass
    prof = cfg / "user-profile.json"
    if prof.exists():
        try:
            d = json.loads(prof.read_text())
            missing = [k for k in ("name", "email") if not d.get(k) or d.get(k) in PLACEHOLDERS]
            (ok if not missing else fail)("profile: name and email", ", ".join(missing),
                                          "fill in ~/.ea/config/user-profile.json")
        except Exception:
            pass
    sp = cfg / "scheduling-preferences.json"
    if sp.exists():
        try:
            d = json.loads(sp.read_text())
            cal = (d.get("ea_managed_calendar") or {}).get("calendar_id", "")
            miss = fail if NEEDS_CALENDAR else warn
            (ok if cal else miss)("EA-Managed calendar",
                                  "configured" if cal else "no calendar_id"
                                  + ("" if NEEDS_CALENDAR else "; optional on this profile"),
                                  "docs/SETUP.md 'The EA-Managed calendar': create it and record its id")
            # The mediator trusts only the owner-private copy of that id;
            # without it every hold and the reconciler's writes are refused.
            if cal:
                sys.path.insert(0, str(REPO / "preprocessor"))
                import owner_trust
                if owner_trust.is_owner_process():
                    priv = owner_trust.private_dir() / owner_trust.CALENDARS_FILE
                    try:
                        owner_doc = json.loads(owner_trust.read_owner_file(priv))
                        owner_cal = owner_doc.get("ea_managed_calendar_id", "")
                    except FileNotFoundError:
                        owner_doc, owner_cal = {}, None
                    seed = "python3 preprocessor/owner_trust.py seed --calendar"
                    if owner_cal is None:
                        miss("EA-Managed calendar (owner copy)", f"{priv} missing", seed)
                    elif owner_cal != cal:
                        fail("EA-Managed calendar (owner copy)",
                             "differs from scheduling-preferences.json",
                             f"fix {priv} or the config so they agree")
                    else:
                        ok("EA-Managed calendar (owner copy)", "matches")
                    if owner_cal and not owner_doc.get("ea_managed_account"):
                        # The account then comes from the resident's config.
                        warn("EA-Managed calendar (owner account)",
                             f"{priv} names no ea_managed_account", seed)
        except Exception:
            pass
    vc = REPO / "tests" / "validate_config.py"
    if vc.exists():
        r = subprocess.run([sys.executable, str(vc)], capture_output=True, text=True,
                           env=dict(os.environ, EA_DIR=str(ea_dir)))
        (ok if r.returncode == 0 else warn)("validate_config.py", (r.stdout or r.stderr).strip().splitlines()[-1][:120]
                                            if (r.stdout or r.stderr).strip() else "",
                                            "python3 tests/validate_config.py and fix the placeholders it names")


def check_install_overlay(ea_dir: Path):
    """The install overlay (preprocessor/install_overlay.py): rendered into
    config, and the same text beside CLAUDE.md in the tree the chat lanes
    run in. Without it the lanes lose the install's own facts."""
    rendered = ea_dir / "config" / "CLAUDE.install.md"
    try:
        home = Path(pwd.getpwnam("ea-bot").pw_dir)
    except KeyError:
        home = None
    # ea-bot runs the render from its own deployed tree: it cannot read a
    # checkout in the owner's home. Before the ea-bot user exists the
    # config directory is the owner's, and the owner renders it directly.
    code = home / "ea-system" if home else REPO
    if home is None:
        render = f"python3 {REPO}/preprocessor/install_overlay.py render (as yourself; the ea-bot user does not exist yet)"
    else:
        # The command the setup docs give (docs/SETUP.md 'The install overlay'); the render
        # finds ea-bot's tree from its passwd home.
        render = f"sudo -u ea-bot /usr/bin/python3 {code}/preprocessor/install_overlay.py render"
    if not rendered.exists():
        warn("install overlay: rendered", "missing", render)
        return
    ok("install overlay: rendered", str(rendered))
    if home is None:
        return
    local = home / "ea-system" / "CLAUDE.local.md"
    install = f"python3 {REPO}/preprocessor/install_overlay.py install --tree {home / 'ea-system'}"
    try:
        same = local.read_text() == rendered.read_text()
    except OSError:
        warn("install overlay: in the lanes' tree", f"{local} missing", install)
        return
    (ok if same else warn)("install overlay: in the lanes' tree",
                           "" if same else "differs from the rendered copy", install)


# The secrets file the owner's processes read first (owner_trust.SECRETS_FILE
# in the owner-private directory). The legacy ~/.ea/.env.secrets is still
# read when it passes the ownership checks, but no fix ever points at it.
SECRETS_NAME = "~/.ea-private/secrets.env"


def _env_fix() -> str:
    """Where ~/.ea/.env comes from on this profile and frontend."""
    graph = ("python3 scripts/install/user_init.py --phase auth (an answers file in the setup skill) writes "
             "the GRAPH_* keys (docs/SETUP.md 'Microsoft Graph app registration')")
    if not NEEDS_SLACK:
        return graph
    if CHAT_FRONTEND == "slack":
        return f"{graph}; the Slack ids go there too (docs/SETUP.md 'The Slack environment files')"
    return f"{graph}; {CHAT_FRONTEND} needs no key there (its settings are the chat block)"


def check_env(ea_dir: Path) -> dict:
    env = _read_env(ea_dir / ".env")
    if not (ea_dir / ".env").exists():
        (fail if NEEDS_SLACK else warn)("~/.ea/.env", "missing", _env_fix())
    for k in REQUIRED_ENV:
        (ok if env.get(k) else fail)(f"env: {k}", "" if env.get(k) else "unset",
                                     "add it to ~/.ea/.env (docs/SETUP.md 'The Slack environment files'); EA_SLACK_OWNER_ID is your Slack member id")
    for k in OPTIONAL_ENV:
        (ok if env.get(k) else warn)(f"env (optional): {k}", "" if env.get(k) else "unset; default applies")
    secrets, source = _owner_secrets(ea_dir)
    if source is None:
        (fail if REQUIRED_SECRETS else warn)(
            SECRETS_NAME, "missing",
            f"put the secrets in {SECRETS_NAME}, mode 600, in a 0700 ~/.ea-private owned by you "
            "(docs/SETUP.md 'Secrets'); ea-bot must not be able to read it")
    elif source != _private_dir() / _owner_trust().SECRETS_FILE:
        warn(SECRETS_NAME, f"only the legacy {source} (in ea-bot's tree)",
             f"move its lines to {SECRETS_NAME} (mode 600); sudo bash deploy/harden-permissions.sh "
             "copies them when that file is missing (docs/SETUP.md 'Secrets')")
    else:
        ok(SECRETS_NAME, str(source))
    for k in REQUIRED_SECRETS:
        (ok if secrets.get(k) else fail)(f"secret: {k}", "" if secrets.get(k) else "unset",
                                         f"docs/SETUP.md 'The Slack app' and 'The app-level token'; put the token in "
                                         f"{SECRETS_NAME} (mode 600)"
                                         if k.startswith("SLACK_") else
                                         f"put {k}=... in {SECRETS_NAME} (mode 600; "
                                         f"scripts/zulip_bootstrap.py prints it)")
    env.update(secrets)
    return env


def _owner_trust():
    import owner_trust
    return owner_trust


def _owner_secrets(ea_dir: Path) -> tuple[dict, Path | None]:
    """The secrets the owner's processes would find, by the owner_trust rule
    without the environment (this shell's environment is not the
    services'): the owner-private secrets.env, then the legacy
    ~/.ea/.env.secrets if it passes the ownership checks. Returns the
    values and the file they came from (None when neither is usable)."""
    ot = _owner_trust()
    priv = _private_dir() / ot.SECRETS_FILE
    legacy = ea_dir / ot.LEGACY_SECRETS_FILE
    for path, check_dir in ((priv, True), (legacy, False)):
        try:
            data = ot.read_owner_file(path, check_dir=check_dir)
        except (OSError, ot.OwnerTrustError):
            continue
        return ot.parse_env(data.decode("utf-8", errors="replace")), path
    return {}, None


def _in_eabot_space(path: str, eabot_home: str | None) -> bool:
    """True when `path`, as written or resolved, lies under ea-bot's home."""
    if not eabot_home:
        return False
    home = os.path.realpath(eabot_home).rstrip("/") + "/"
    raw = eabot_home.rstrip("/") + "/"
    return path.startswith(raw) or os.path.realpath(path).startswith(home)


def _unit_env_files(unit: str) -> list[str] | None:
    """The EnvironmentFile paths systemd loads for `unit`; None when the
    unit is not installed."""
    def show(prop):
        return subprocess.run(["systemctl", "show", "-p", prop, "--value", unit],
                              capture_output=True, text=True).stdout.strip()
    if show("LoadState") in ("", "not-found"):
        return None
    # EnvironmentFiles value: "/a (ignore_errors=no)" one per line
    return re.findall(r"(/\S+?) \(ignore_errors=", show("EnvironmentFiles"))


def check_owner_trust(ea_dir: Path):
    """Each item an owner process trusts, reported as resolving outside
    ea-bot space or passing the ownership checks of
    preprocessor/owner_trust.py (owned by the user running the doctor, not
    a symlink, writable by no other user): the units' environment files,
    the secrets, the chat trust settings, the auth directory, the
    scheduled-send key and the token files. Run it as the owner."""
    try:
        ot = _owner_trust()
    except Exception as e:
        warn("owner trust", f"preprocessor/owner_trust.py not importable: {e}")
        return
    fix_root = "sudo bash deploy/harden-permissions.sh"
    try:
        eabot_home = pwd.getpwnam("ea-bot").pw_dir
    except KeyError:
        eabot_home = None

    if shutil.which("systemctl"):
        for unit, label in (("ea-chat-bot", "bot unit"), ("ea-mail-mediator", "mediator unit")):
            name = f"owner trust: {label} environment files"
            paths = _unit_env_files(unit)
            if paths is None:
                continue
            bad = []
            for path in paths:
                if _in_eabot_space(path, eabot_home):
                    bad.append(f"{path} is in ea-bot space (systemd loads it unchecked)")
                    continue
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue
                if st.st_uid == 0 and not st.st_mode & 0o022 and not stat.S_ISLNK(st.st_mode):
                    continue  # root-owned and writable by root only
                try:
                    ot.check_owner_file(path)
                except ot.OwnerTrustError as e:
                    bad.append(str(e))
            if bad:
                warn(name, "; ".join(bad), fix_root)
            else:
                ok(name, ", ".join(paths) or "none")

    # Metadata only: the checks never need a secret's contents.
    priv_sec = _private_dir() / ot.SECRETS_FILE
    legacy = ea_dir / ot.LEGACY_SECRETS_FILE
    why = []
    source = None
    for path, is_private in ((priv_sec, True), (legacy, False)):
        try:
            if is_private:
                ot.check_owner_dir(path.parent)
            ot.check_owner_file(path)
        except FileNotFoundError:
            continue
        except (OSError, ot.OwnerTrustError) as e:
            why.append(str(e))
            continue
        source = path
        break
    if source == priv_sec:
        ok("owner trust: secrets", f"{priv_sec} (owner-private)")
    elif source == legacy:
        warn("owner trust: secrets", f"only the legacy {legacy} (ea-bot space; passes the ownership checks)",
             fix_root)
    elif why:
        fail("owner trust: secrets", "; ".join(why), fix_root)

    if NEEDS_SLACK:
        chat = _private_dir() / ot.CHAT_FILE
        try:
            block = json.loads(ot.read_owner_file(chat))
            if not isinstance(block, dict):
                raise ot.OwnerTrustError(f"{chat} does not hold a JSON object")
        except FileNotFoundError:
            fail("owner trust: chat settings",
                 "no chat.json in the private dir; the bot and the owner's jobs refuse to "
                 "run the chat frontend until it exists",
                 "python3 preprocessor/owner_trust.py seed --chat (as the owner, at a terminal)")
        except (OSError, ValueError, ot.OwnerTrustError) as e:
            fail("owner trust: chat settings", str(e)[:200], f"fix or recreate {chat} (mode 600)")
        else:
            z = block.get("zulip") if isinstance(block.get("zulip"), dict) else {}
            w = block.get("web") if isinstance(block.get("web"), dict) else {}
            ok("owner trust: chat settings",
               f"{chat}: frontend web, public_url {w.get('public_url', '')}, "
               f"login {w.get('tailscale_login', '')}" if block.get("frontend") == "web" else
               f"{chat}: frontend {block.get('frontend', 'slack')}, site {z.get('site', '')}, "
               f"bot {z.get('bot_email', '')}, owner {z.get('owner_user_id', '')}")
            shared = _read_config_chat(ea_dir)
            sz = shared.get("zulip") if isinstance(shared.get("zulip"), dict) else {}
            differ = [k for k in ("site", "bot_email", "owner_user_id") if z.get(k, "") != sz.get(k, "")]
            if block.get("frontend", "slack") != shared.get("frontend", "slack"):
                differ.insert(0, "frontend")
            if differ:
                warn("owner trust: chat settings copy",
                     f"ea-config.json differs from chat.json in {', '.join(differ)} "
                     "(the owner uses chat.json; the resident's copy shapes wording only)",
                     "scripts/zulip_switch.sh writes both copies; or edit ea-config.json to match")

    auth = ea_dir / "auth"
    if not auth.exists():
        return
    try:
        ot.check_owner_dir(auth, allow_private_group=True)
        ok("owner trust: auth dir", f"{auth}: the owner's, writable by no other user")
    except (OSError, ot.OwnerTrustError) as e:
        fail("owner trust: auth dir", str(e)[:200],
             f"a mode only: chmod 750 {auth}. Another user's: never chown it; move it aside, "
             f"make a new one as the owner, sign in again and make a new scheduled-send key "
             f"(docs/SETUP.md 'The deferred-send signing key')")
        return
    key = auth / "scheduled-send-hmac.key"
    if key.exists():
        try:
            ot.check_owner_dir(auth)
            ot.check_owner_file(key)
            ok("owner trust: scheduled-send key", "passes the ownership checks")
        except (OSError, ot.OwnerTrustError) as e:
            fail("owner trust: scheduled-send key", f"{str(e)[:200]} (scheduled sends are held)",
                 f"chmod 600 {key} as the owner; a key another user owns: delete it and make a new one "
                 f"(docs/SETUP.md 'The deferred-send signing key'), never chown it")
    tokens = [auth / n for n in ("msal_token_cache.json", "msal_tokens.json")]
    tokens += sorted(auth.glob("google_token_*.json"))
    # Plain-text tokens an owner plugin's harvest writes (owner_trust.HARVESTED_TOKEN_GLOB).
    harvested = sorted(auth.glob(ot.HARVESTED_TOKEN_GLOB))
    tokens += harvested
    bad = []
    for t in tokens:
        if not t.exists():
            continue
        try:
            ot.check_owner_file(t, allow_private_group=True)
        except (OSError, ot.OwnerTrustError) as e:
            bad.append(str(e))
    if bad:
        fail("owner trust: token files", "; ".join(bad)[:300],
             f"as the owner, chmod 600 the files named in {auth}; a token another user owns: "
             f"delete it and sign in again, never chown it")
    else:
        ok("owner trust: token files", f"{sum(t.exists() for t in tokens)} file(s) pass the ownership checks")
    if harvested:
        mediated = _mediation_on(ea_dir)
        loose = [f"{t.name} {oct(t.lstat().st_mode & 0o777)}" for t in harvested
                 if t.lstat().st_mode & (0o077 if mediated else 0o022)]
        if loose and mediated:
            warn("owner trust: harvested token modes",
                 f"{', '.join(loose)} with mail mediation on; only the owner's mediator reads them",
                 f"{fix_root} (or, as the owner: python3 preprocessor/owner_trust.py token-modes)")
        elif loose:
            fail("owner trust: harvested token modes", f"{', '.join(loose)}: writable by group or others",
                 "python3 preprocessor/owner_trust.py token-modes  (as the owner)")
        else:
            ok("owner trust: harvested token modes",
               f"{len(harvested)} file(s) "
               f"({'mediated' if mediated else 'no mediation: group read for the resident'})")
    # A browser profile (SSO cookies) belongs in the owner-private dir: a
    # browser writes its profile by path for a whole session, which no check
    # made before launch can cover under a directory the resident can rename.
    # Owner plugins keep theirs there; a directory left in auth/ is one an
    # older install kept.
    legacy = sorted(d.name for d in auth.iterdir()
                    if d.is_dir() and not d.is_symlink()
                    and not os.path.lexists(_private_dir() / d.name))
    if legacy:
        warn("owner trust: browser profiles",
             f"{', '.join(legacy)} still under {auth}, a directory the resident can rename",
             f"move each to {_private_dir()} as the owner (the plugin that uses it says how)")
    else:
        ok("owner trust: browser profiles", f"none under {auth}")


def _read_config_chat(ea_dir: Path) -> dict:
    try:
        doc = json.loads((ea_dir / "config" / "ea-config.json").read_text())
    except (OSError, ValueError):
        return {}
    chat = doc.get("chat") if isinstance(doc, dict) else None
    return chat if isinstance(chat, dict) else {}


def _mediation_on(ea_dir: Path) -> bool:
    """mail_mediation.mediation_active_for_resident() for this EA tree."""
    try:
        import mail_mediation as mm
        mm.EA_DIR = ea_dir
        mm.CONFIG = ea_dir / "config" / "ea-config.json"
        return bool(mm.mediation_active_for_resident())
    except Exception:
        return True


def _trusted_chat_block() -> dict | None:
    """The owner-private chat.json when it passes the checks, else None."""
    try:
        ot = _owner_trust()
        block = json.loads(ot.read_owner_file(_private_dir() / ot.CHAT_FILE))
    except Exception:
        return None
    return block if isinstance(block, dict) else None


def _graph_auth_sources() -> list[str]:
    """The enabled owner plugins that declare a graph auth source, from the
    owner's lock ([] outside an owner process, which cannot read it)."""
    try:
        import plugin_registry
        return [lp.name for lp in plugin_registry.managed_plugins()
                if any(a.get("kind") == "graph"
                       for a in lp.manifest["capabilities"]["owner_hooks"]["auth_sources"])]
    except Exception:
        return []


def _backends(ea_dir: Path) -> dict:
    """mail.backend, calendar.backend and availability.provider as an owner
    process reads them (native_accounts.backends)."""
    try:
        import native_accounts
        return native_accounts.backends(ea_dir / "config" / "ea-config.json")
    except Exception:
        return {"mail": "graph", "calendar": "graph", "availability": "graph"}


def check_native_accounts(ea_dir: Path) -> None:
    """The macOS native-accounts backend (docs/macos-native-accounts.md): one
    PASS row when no backend is macos; else the helper agent, its three
    permissions, the Mail accounts and calendars the scope names, and the
    availability provider."""
    try:
        import native_accounts
        rows = native_accounts.doctor_rows(
            config_path=ea_dir / "config" / "ea-config.json",
            scope_file=_private_dir() / native_accounts.SCOPE_FILE)
    except Exception as e:
        warn("native accounts (macOS)", f"check failed: {e}")
        return
    for r in rows:
        rec(r["status"], r["name"], r["detail"], r["fix"])


def check_auth(ea_dir: Path):
    auth = ea_dir / "auth"
    b = _backends(ea_dir)
    if b["mail"] == "macos" and b["calendar"] == "macos" and b["availability"] != "graph":
        # Mail and calendars come through the Mac's own accounts, and other
        # people's free/busy through the outlook-web-auth token or nowhere:
        # no Graph or Google token is needed (check_native_accounts).
        ok("auth: graph and google tokens",
           f"not needed (mail and calendar on macos, availability {b['availability']})")
        key = auth / "scheduled-send-hmac.key"
        (ok if key.exists() else warn)("auth: scheduled-send-hmac.key", "" if key.exists() else "missing",
                                       "docs/SETUP.md 'The deferred-send signing key'")
        return
    p = auth / "msal_token_cache.json"
    sources = [] if p.exists() else _graph_auth_sources()
    if sources:
        # No MSAL cache: an owner plugin's auth source supplies the token
        # (docs/plugins.md). Ask it for one now.
        try:
            import plugin_registry
            good = any(True for _ in plugin_registry.owner_auth_tokens("graph"))
        except Exception:
            good = False
        (ok if good else fail)("auth: graph token source",
                               f"{', '.join(sources)}: "
                               f"{'a token now' if good else 'no valid token now'}",
                               "the plugin's README says how to refresh its token")
    elif p.exists():
        good, why = _json_ok(p)
        (ok if good else fail)("auth: msal_token_cache.json", why or "parses",
                               "docs/SETUP.md 'Microsoft Graph app registration'; a torn file: keep its first JSON object (graph_client does this on load)")
    else:
        fail("auth: msal_token_cache.json", "missing", "docs/SETUP.md 'Microsoft Graph app registration', then python3 preprocessor/graph_actions.py list-mail --folder inbox --top 1")
    toks = sorted(auth.glob("google_token_*.json")) if auth.exists() else []
    (ok if toks else warn)("auth: google tokens", f"{len(toks)} account file(s)",
                           "docs/SETUP.md 'Google OAuth app': google_calendar_client.py --setup for each Google account in scheduling-preferences.json")
    key = auth / "scheduled-send-hmac.key"
    (ok if key.exists() else warn)("auth: scheduled-send-hmac.key", "" if key.exists() else "missing",
                                   "docs/SETUP.md 'The deferred-send signing key'")


GRAPH_AUTH_METHODS = ("", "confidential", "device_code", "interactive")
OUTLOOK_WEB_PLUGIN = "https://github.com/arpg/tacitus-plugin-outlook-web-auth"
OUTLOOK_WEB_FIX = (f"install the owner plugin outlook-web-auth ({OUTLOOK_WEB_PLUGIN}, README "
                   "'Moving from the core's copy'), or move to an app registration "
                   "(docs/AUTHENTICATION.md, Option A or B)")


def _owner_crontab() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    return r.stdout if r.returncode == 0 else ""


def check_outlook_web_path(env: dict, *, cron_text: str | None = None,
                           sources: list[str] | None = None):
    """An install set up for the Outlook web token path the core carried
    before v0.1.0: GRAPH_AUTH_METHOD=playwright, or a crontab line that
    runs the removed token_refresh.py. Without an owner plugin that
    supplies a Graph token, Outlook access stops when the last harvested
    token expires. Silent on an install with neither sign."""
    method = (env.get("GRAPH_AUTH_METHOD") or "").strip().lower()
    cron_text = _owner_crontab() if cron_text is None else cron_text
    old_cron = any("token_refresh.py" in ln and not ln.lstrip().startswith("#")
                   for ln in cron_text.splitlines())
    if method not in GRAPH_AUTH_METHODS and method != "playwright":
        warn("auth: GRAPH_AUTH_METHOD", f"{method!r} is not read",
             "use confidential, device_code or interactive, or remove it (docs/AUTHENTICATION.md)")
    if method != "playwright" and not old_cron:
        return
    sources = _graph_auth_sources() if sources is None else sources
    if not sources:
        signs = (["GRAPH_AUTH_METHOD=playwright"] if method == "playwright" else []) + \
                (["a crontab line runs token_refresh.py"] if old_cron else [])
        fail("auth: outlook web token path",
             "; ".join(signs) + ": v0.1.0 moved this path to an owner plugin, and none supplies a Graph token",
             OUTLOOK_WEB_FIX)
    elif old_cron:
        fail("auth: outlook web token path",
             f"{', '.join(sources)} supplies the token, but a crontab line still runs the removed token_refresh.py",
             "replace that line with the one `ea-plugin.py cron-lines` prints")
    else:
        ok("auth: outlook web token path",
           f"{', '.join(sources)} supplies the token; GRAPH_AUTH_METHOD=playwright is no longer read")


def check_slack(env: dict):
    tok = env.get("SLACK_BOT_TOKEN", "")
    if not tok:
        return
    try:
        import urllib.request
        req = urllib.request.Request("https://slack.com/api/auth.test", method="POST", data=b"",
                                     headers={"Authorization": f"Bearer {tok}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
            scopes = set((r.headers.get("x-oauth-scopes") or "").split(","))
    except Exception as e:
        fail("slack: auth.test", str(e)[:120], "check the network and SLACK_BOT_TOKEN")
        return
    if not body.get("ok"):
        fail("slack: auth.test", body.get("error", "?"), "regenerate the bot token (docs/SETUP.md 'The Slack app')")
        return
    ok("slack: auth.test", f"team {body.get('team_id')} bot {body.get('user_id')}")
    missing = [s for s in REQUIRED_SLACK_SCOPES if s not in scopes]
    (ok if not missing else fail)("slack: bot scopes", "all present" if not missing else "missing " + ", ".join(missing),
                                  "api.slack.com/apps > OAuth & Permissions > Bot Token Scopes; then reinstall (docs/SETUP.md 'The Slack app')")
    cid = env.get("EA_SLACK_CHANNEL_ID", "")
    if cid:
        try:
            req = urllib.request.Request("https://slack.com/api/conversations.info", method="POST",
                                         data=f"channel={cid}".encode(),
                                         headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.loads(r.read())
            ch = info.get("channel") or {}
            if not info.get("ok"):
                fail("slack: EA channel", info.get("error", "?"), "invite the bot to the channel; check EA_SLACK_CHANNEL_ID")
            elif not ch.get("is_private"):
                fail("slack: EA channel", f"#{ch.get('name')} is PUBLIC", "the EA channel must be private (docs/SETUP.md 'The private channel')")
            elif not ch.get("is_member"):
                fail("slack: EA channel", f"bot is not a member of #{ch.get('name')}", "/invite @EA in the channel")
            else:
                ok("slack: EA channel", f"#{ch.get('name')} private, bot is a member")
        except Exception as e:
            warn("slack: EA channel", str(e)[:100])


def check_zulip(env: dict, zcfg: dict):
    key = env.get("ZULIP_API_KEY", "")
    if not key:
        return
    site, email = zcfg.get("site", ""), zcfg.get("bot_email", "")
    if not site or not email:
        fail("zulip: config", "chat.zulip.site or chat.zulip.bot_email unset",
             "paste the chat block scripts/zulip_bootstrap.py prints into ~/.ea/config/ea-config.json")
        return
    try:
        import zulip
    except ImportError:
        # The bot runs from the repo venv; this interpreter may be the
        # system python3. A package that is in the venv is not missing.
        venv_py = REPO / ".venv" / "bin" / "python3"
        if venv_py.exists() and subprocess.run([str(venv_py), "-c", "import zulip"],
                                                capture_output=True).returncode == 0:
            warn("zulip: client", f"package 'zulip' is in {venv_py.parent.parent}, not in "
                 f"{sys.executable}; the live check needs the venv interpreter",
                 f"rerun as {venv_py} scripts/ea-doctor.py")
            return
        fail("zulip: client", "python package 'zulip' missing", f"{REPO}/.venv/bin/pip install zulip")
        return
    try:
        client = zulip.Client(email=email, api_key=key, site=site)
        prof = client.get_profile()
    except Exception as e:
        fail("zulip: get_profile", str(e)[:120], "check the network, chat.zulip.site and ZULIP_API_KEY")
        return
    if prof.get("result") != "success":
        fail("zulip: get_profile", prof.get("msg", "?"), "regenerate the bot's API key and update ZULIP_API_KEY")
        return
    ok("zulip: get_profile", f"bot {prof.get('email')} id {prof.get('user_id')}")
    try:
        subs = client.get_subscriptions()
    except Exception as e:
        warn("zulip: subscriptions", str(e)[:100])
        return
    if subs.get("result") != "success":
        fail("zulip: subscriptions", subs.get("msg", "?"), "check the bot's permissions")
        return
    by_name = {s.get("name"): s for s in subs.get("subscriptions", [])}
    for label, name, required in (("EA stream", zcfg.get("stream", ""), True),
                                  ("scheduling stream", zcfg.get("sched_stream", ""), False),
                                  ("DM stream", zcfg.get("dm_stream", ""), True)):
        check = f"zulip: {label}"
        if not name:
            (fail if required else ok)(check, "unset" if required else "not configured",
                                       "set it in chat.zulip of ea-config.json")
            continue
        sub = by_name.get(name)
        if sub is None:
            try:
                found = client.get_stream_id(name).get("result") == "success"
            except Exception:
                found = False
            if found:
                fail(check, f"bot is not subscribed to #{name}", "subscribe the bot to the stream (scripts/zulip_bootstrap.py)")
            else:
                fail(check, f"#{name} does not exist or is hidden from the bot",
                     "create it invite-only with the bot subscribed (scripts/zulip_bootstrap.py)")
        elif not sub.get("invite_only"):
            fail(check, f"#{name} is PUBLIC", "the stream must be private (invite-only)")
        else:
            ok(check, f"#{name} private, bot is subscribed")


def check_chat(env: dict):
    """The chat platform checks for the configured frontend, and the web
    chat's when it runs alone or as the shadow (both are reported)."""
    if CHAT_FRONTEND == "slack":
        check_slack(env)
    elif CHAT_FRONTEND == "zulip":
        check_zulip(env, CHAT_CFG.get("zulip") or {})
    elif CHAT_FRONTEND == "memory":
        if not NEEDS_SLACK:
            return
        warn("chat: memory frontend", "checks skipped; the memory frontend is for tests",
             "set chat.frontend to web, zulip or slack in ~/.ea/config/ea-config.json")
    elif CHAT_FRONTEND != "web":
        fail("chat: frontend", f"unknown frontend {CHAT_FRONTEND!r}",
             "set chat.frontend to web, zulip or slack in ~/.ea/config/ea-config.json")
    if NEEDS_SLACK and "web" in (CHAT_FRONTEND, CHAT_CFG.get("shadow")):
        check_web(CHAT_CFG.get("web") if isinstance(CHAT_CFG.get("web"), dict) else {})


# The HTTPS port docs/SETUP.md "Web chat" publishes the chat on, and the
# shortest pairing token the bot takes (botcore/botcore/chat/web_frontend.py).
WEB_SERVE_PORT = 8444
WEB_MIN_TOKEN = 16


def _web_private_problem(path: Path, kind: str) -> str | None:
    """None when `path` is a `kind` ("dir" or "file") this user owns, not a
    symlink, with no permission for group or others; else what is wrong
    (the bot's own rule, web_frontend.private_problem). Metadata only."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symlink"
    if not (stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode)):
        return f"{path} is not a {'directory' if kind == 'dir' else 'regular file'}"
    if st.st_uid != os.geteuid():
        return f"{path} is owned by uid {st.st_uid}, not by you"
    if st.st_mode & 0o077:
        return f"{path} has mode {oct(st.st_mode & 0o777)}; it must be {'0700' if kind == 'dir' else '0600'}"
    return None


def check_web(wcfg: dict) -> None:
    """The web chat's block as the bot serves it (docs/webchat.md, Config
    and Identity and auth), in three rows: the settings (from the
    owner-private chat.json, a port, a base path, and a public_url with the
    chat's own HTTPS port under that path), the sign-in (the Tailscale
    login, or a private pairing token) and the store (a directory that is
    yours alone, outside the resident's tree). Each row passes or warns
    with its fix. Nothing is written and no secret is read."""
    try:
        ot = _owner_trust()
    except Exception as e:
        warn("web chat: settings", f"preprocessor/owner_trust.py not importable: {e}")
        return
    chat_json = _private_dir() / ot.CHAT_FILE
    restart = "then sudo systemctl restart ea-chat-bot (docs/SETUP.md 'Web chat')"
    roots = ot.resident_roots()

    # the settings
    trusted = _trusted_chat_block()
    if trusted is None or not ot.runs_web(trusted):
        warn("web chat: settings",
             f"the web block comes from ea-config.json; the bot serves the web chat only "
             f"from {chat_json}",
             "python3 preprocessor/owner_trust.py seed --chat (as yourself, at a terminal)")
    else:
        why, fixes = [], []
        port = wcfg.get("port", 8470)
        if not (str(port).isdigit() and 0 < int(port) < 65536):
            why.append(f"port {port!r} is not a port number")
            fixes.append("set web.port to 8470")
        base = "/" + str(wcfg.get("base_path") if wcfg.get("base_path") is not None
                         else "/chat").strip().strip("/")
        base = "" if base == "/" else base
        if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)*", base):
            why.append(f"base_path {wcfg.get('base_path')!r} is not a plain path")
            fixes.append("set web.base_path to /chat")
        url = str(wcfg.get("public_url") or "").strip().rstrip("/")
        example = f"https://<host>.<tailnet>.ts.net:{WEB_SERVE_PORT}{base}"
        if not url:
            why.append("no public_url")
            fixes.append(f"set web.public_url to {example}, the address `tailscale serve "
                         f"--https={WEB_SERVE_PORT}` gives")
        else:
            import urllib.parse
            try:
                parts = urllib.parse.urlsplit(url)
                url_port = parts.port
            except ValueError:
                parts, url_port = None, None
            if parts is None or parts.scheme != "https" or not parts.hostname:
                why.append(f"public_url {url} is not an https address")
                fixes.append(f"set web.public_url to {example}")
            elif url_port is None:
                why.append(f"public_url {url} has no port; the chat needs an HTTPS port of "
                           f"its own (one app per origin)")
                fixes.append(f"add the `tailscale serve --https` port: {example}")
            elif parts.path.rstrip("/") != base:
                why.append(f"public_url's path {parts.path or '/'} is not the base_path "
                           f"{base or '/'}")
                fixes.append(f"end web.public_url with {base or '/'}")
        if why:
            warn("web chat: settings", "; ".join(why),
                 f"{'; '.join(fixes)} in {chat_json}, {restart}")
        else:
            ok("web chat: settings", f"{url} serves 127.0.0.1:{int(port)}{base} (from {chat_json})")

    store = Path(str(wcfg.get("store_path") or "") or _private_dir() / "webchat.sqlite")
    store_dir = store.parent

    # the sign-in
    token = Path(str(wcfg.get("pair_token_file") or "") or store_dir / "webchat-pair.token")
    login = str(wcfg.get("tailscale_login") or "").strip()
    trust = wcfg.get("trust_tailscale_header", True)
    header_on = trust is not False and str(trust).strip().lower() not in ("false", "0", "no", "off", "")
    token_why = None
    inside = ot.inside_roots(token, roots)
    if inside is not None:
        token_why = f"the pairing token file {token} is inside {inside}, which the resident controls"
    elif os.path.lexists(token):
        token_why = _web_private_problem(token, "file")
        if token_why is None and os.lstat(token).st_size < WEB_MIN_TOKEN:
            token_why = f"the pairing token in {token} is shorter than {WEB_MIN_TOKEN} characters"
    if token_why:
        warn("web chat: sign-in", f"{token_why}; the bot does not serve the web chat with it",
             f"leave web.pair_token_file empty in {chat_json}, or replace the file with a token "
             f"of {WEB_MIN_TOKEN} or more characters, mode 600, yours; {restart}")
    elif login and header_on:
        ok("web chat: sign-in", f"Tailscale login {login}"
           + (f"; pairing token at {token}" if os.path.lexists(token) else ""))
    elif os.path.lexists(token):
        ok("web chat: sign-in", f"pairing token at {token}")
    else:
        warn("web chat: sign-in",
             f"no way to sign in yet: no tailscale_login{'' if header_on else ' (header off)'} "
             f"and no pairing token at {token}",
             f"set web.tailscale_login in {chat_json} to your login (`tailscale whois` shows "
             f"it), or start the bot once: it writes the pairing token there; {restart}")

    # the store
    inside = ot.inside_roots(store, roots)
    if inside is not None:
        warn("web chat: store", f"{store} is inside {inside}, which the resident controls",
             f"leave web.store_path empty (the owner-private directory) in {chat_json}; "
             f"{restart}")
        return
    if not os.path.lexists(store_dir):
        warn("web chat: store", f"{store_dir} does not exist",
             f"mkdir -m 700 {store_dir}, or leave web.store_path empty in {chat_json}")
        return
    why = _web_private_problem(store_dir, "dir")
    if why is None and os.path.lexists(store):
        why = _web_private_problem(store, "file")
    if why:
        warn("web chat: store", f"{why}; the bot does not serve the web chat from it",
             f"chmod 700 {store_dir}; chmod 600 {store} (both must be yours; never chown "
             f"another user's file, move it aside)")
    else:
        ok("web chat: store", f"{store} ({'present' if os.path.lexists(store) else 'made on the bot start'}, "
           f"in a directory only you can open)")


def _launchd_jobs() -> tuple:
    import launchd_labels
    return ((launchd_labels.label("ea-chat-bot"), True),
            (launchd_labels.label("ea-mail-mediator"), False))
LAUNCHD_PLIST = "/Library/LaunchDaemons/{}.plist"


def check_launchd_services():
    """macOS: the LaunchDaemons deploy/macos/install-services.sh installs."""
    for label, required in _launchd_jobs():
        r = subprocess.run(["launchctl", "print", f"system/{label}"], capture_output=True, text=True)
        pid = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
        if r.returncode == 0 and pid:
            ok(f"service: {label}", f"pid {pid.group(1)}")
        elif required and NEEDS_SLACK:
            fail(f"service: {label}", "not running" if r.returncode == 0 else "not loaded",
                 "sudo bash deploy/macos/install-services.sh; logs: ~/.ea-private/logs/*.launchd.log")
        else:
            warn(f"service: {label}", "not running" if r.returncode == 0 else "not loaded",
                 "required only when mail mediation is on (deploy/enable-mail-mediation.sh)"
                 if "mediator" in label else "the chat bot; not used by this profile")


def check_launchd_hardening():
    """macOS counterpart of check_hardening: each plist root-owned, its
    program under /opt/ea-system, and no env file it loads writable by ea-bot."""
    import plistlib
    try:
        eabot = pwd.getpwnam("ea-bot")
    except KeyError:
        return
    fix = "sudo bash deploy/macos/install-services.sh"
    for label, _ in _launchd_jobs():
        path = Path(LAUNCHD_PLIST.format(label))
        if not path.exists():
            continue
        st = path.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            warn(f"hardening: {label}", f"{path} is not root-owned and closed to writes", fix)
            continue
        args = plistlib.loads(path.read_bytes()).get("ProgramArguments") or []
        env_files = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--env"]
        program = args[args.index("--") + 2] if "--" in args and len(args) > args.index("--") + 2 else ""
        if not program.startswith("/opt/ea-system/") or eabot.pw_dir + "/" in " ".join(args):
            warn(f"hardening: {label}", f"runs {program or '?'}; expected /opt/ea-system, nothing in ea-bot space", fix)
            continue
        bad = []
        for f in env_files:
            try:
                fst = os.stat(f)
            except OSError:
                continue  # an absent env file is skipped by the launcher
            if fst.st_uid == eabot.pw_uid or fst.st_mode & 0o022:
                bad.append(f)
        if bad:
            warn(f"hardening: {label}", f"loads env files ea-bot could rewrite: {', '.join(bad)}", fix)
        else:
            ok(f"hardening: {label}", "root-owned plist, code in /opt/ea-system, owner-private env")


def check_services():
    if sys.platform == "darwin":
        check_launchd_services()
        return
    if not shutil.which("systemctl"):
        warn("systemd", "not available", "docs/SETUP.md 'Services'")
        return
    user_unit = Path.home() / ".config/systemd/user/ea-chat-bot.service"
    if user_unit.exists():
        warn("service: ea-chat-bot user unit", f"{user_unit} exists; the bot is the system unit",
             "systemctl --user disable --now ea-chat-bot, remove the file, systemctl --user "
             "daemon-reload, then sudo bash deploy/harden-permissions.sh (docs/SETUP.md 'Services')")
    for unit, required in SERVICES:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        state = r.stdout.strip()
        if state == "active":
            ok(f"service: {unit}")
        elif required and NEEDS_SLACK:
            fail(f"service: {unit}", state or "unknown",
                 "sudo bash deploy/harden-permissions.sh writes and starts the system unit "
                 "(docs/SETUP.md 'Services')")
        elif required:
            warn(f"service: {unit}", state or "not installed", "the Slack bot; not used by this profile")
        else:
            warn(f"service: {unit}", state or "not installed",
                 {"ea-dashboard": "optional web board (deploy/ea-dashboard.service); not needed for reports",
                  "ea-mail-mediator": "required only when mail mediation is on (docs/mail-mediation-spec.md)"}[unit])


def check_resident_trust(ea_dir: Path):
    """The resident must not pass owner_trust.is_owner_process(): if it
    did, the owner-only checks would run as ea-bot (ledger.py refuses a
    database sidecar the owner's processes created) and the owner's
    private paths would resolve into ea-bot's home. Asked of the resident
    itself through the sudo rule the mediator uses; skipped when that
    rule is absent or the deployed tree is not there."""
    try:
        eabot_home = Path(pwd.getpwnam("ea-bot").pw_dir)
    except KeyError:
        return
    code = eabot_home / "ea-system" / "preprocessor" / "owner_trust.py"
    if not code.exists() or not shutil.which("sudo"):
        return
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import owner_trust as ot; "
             "print('owner' if ot.is_owner_process() else 'resident', ot.private_dir())")
    try:
        r = subprocess.run(["sudo", "-n", "-u", "ea-bot", "/usr/bin/python3", "-I", "-c", probe,
                            str(code.parent)],
                           capture_output=True, text=True, timeout=30, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0:
        return  # no sudo rule for ea-bot here, or no python: nothing to report
    answer = r.stdout.strip()
    if answer.startswith("resident"):
        ok("resident trust", "ea-bot is not an owner process")
    else:
        fail("resident trust", f"ea-bot passes the owner test ({answer})",
             "preprocessor/owner_trust.py is_owner_process(): a .ea-private in ea-bot's home "
             "must not make it the owner; redeploy the current tree")


def check_hardening():
    """The owner's services must not run code or load an environment file from
    the ea-bot user's tree (deploy/harden-permissions.sh). ea-bot can rewrite
    its own tree, so a service that trusts a path there is an escalation route.
    A WARN, not a FAIL: the EA runs without it, but a real install should have
    it (see docs/SECURITY.md, "Owner services off the ea-bot tree")."""
    if sys.platform == "darwin":
        check_launchd_hardening()
        return
    if not shutil.which("systemctl"):
        return
    import pwd
    try:
        eabot_home = pwd.getpwnam("ea-bot").pw_dir
    except KeyError:
        return  # no ea-bot user: check_users_and_dirs already failed
    r = subprocess.run(["systemctl", "is-active", "ea-chat-bot"],
                       capture_output=True, text=True)
    if r.stdout.strip() not in ("active", "activating", "reloading"):
        return  # service not installed/running: check_services covers that
    def _show(prop):
        return subprocess.run(["systemctl", "show", "-p", prop, "--value", "ea-chat-bot"],
                              capture_output=True, text=True).stdout.strip()
    import re as _re
    import stat as _stat
    try:
        eabot_uid = pwd.getpwnam("ea-bot").pw_uid
    except KeyError:
        return

    def _ea_writable(path: str) -> bool:
        # An env file ea-bot can rewrite: owned by ea-bot, or group-writable
        # by a group ea-bot is in. Owner-owned files under ea-bot's home
        # (e.g. .env.secrets, <owner>:<owner> 600) are safe and do not count.
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_uid == eabot_uid:
            return True
        return bool(st.st_mode & _stat.S_IWGRP)  # conservative: any group write

    execstart = _show("ExecStart")
    # EnvironmentFiles value: "/a (ignore_errors=no) /b (ignore_errors=yes)"
    env_paths = _re.findall(r"(/\S+?) \(ignore_errors=", _show("EnvironmentFiles"))
    fix = "sudo bash deploy/harden-permissions.sh"
    if f"{eabot_home}/" in execstart:
        warn("hardening: service code path",
             "ea-chat-bot runs code from the ea-bot tree (ea-bot owns the parent)", fix)
    elif any(_ea_writable(p) for p in env_paths):
        warn("hardening: service env file",
             "ea-chat-bot loads an env file ea-bot can rewrite", fix)
    else:
        ok("hardening: services off the ea-bot tree")


def check_decks(ea_dir: Path):
    """The optional deck lane (the owner's slidev-decks plugin). WARN when it is not
    installed; FAIL only on a half install, which is what would surface to
    the user as a build that dies mid-way."""
    ref = Path("/srv/ea/presentations")
    try:
        bot_home = Path(pwd.getpwnam("ea-bot").pw_dir)
    except KeyError:
        return
    node = bot_home / ".local" / "node" / "bin" / "node"
    template = bot_home / "presentations" / "decks" / "_template" / "node_modules"
    fix = "sudo bash deploy/enable-deck-builder.sh"
    mounted = (ref / "shared" / "scripts" / "build-deck.sh").exists()
    if not mounted and not node.exists():
        warn("deck builder", "not installed (optional)", fix)
        return
    (ok if mounted else fail)("deck builder: owner's presentations mounted read-only", str(ref), fix)
    (ok if node.exists() else fail)("deck builder: node for ea-bot", str(node), fix)
    (ok if template.is_dir() else warn)("deck builder: toolchain template",
                                        "" if template.is_dir() else "missing", fix)
    if mounted:
        try:
            opts = subprocess.run(["findmnt", "-no", "OPTIONS", str(ref)],
                                  capture_output=True, text=True).stdout
            (ok if "ro" in opts.split(",") else fail)("deck builder: mount is read-only", opts.strip(), fix)
        except OSError:
            pass
    for name in ("tailscale", "curl"):
        shim = REPO / "scripts" / "deck-bin" / name
        (ok if os.access(shim, os.X_OK) else fail)(f"deck builder: {name} shim executable", str(shim),
                                                   f"chmod 755 {shim}")


def check_remote_llm():
    """The optional remote-LLM layer (docs/llm-lanes.md): the email writer
    and, beyond it, the mail gate, digests, briefings and scheduling agent
    on an OpenAI-compatible endpoint. Off is the normal state; on, it says
    which switches are in force and FAILs when one is on with no key."""
    sys.path.insert(0, str(REPO / "preprocessor"))
    try:
        import remote_llm
    except Exception as e:
        warn("remote LLM (optional)", f"module unreadable: {e}")
        return
    cfg = remote_llm.config()
    live = {k: v["effective"] for k, v in remote_llm.switches(cfg).items()
            if v["effective"] != "off"}
    if not live:
        ok("remote LLM (optional)", "off")
        return
    key = bool(remote_llm.api_key())
    detail = ", ".join(f"{k} {v}" for k, v in live.items())
    if cfg.get("shadow_until") and any(v == "shadow" for v in live.values()):
        detail += f"; shadow until {cfg['shadow_until']}"
    detail = f"{detail} ({cfg['model']} at {cfg['endpoint']})"
    (ok if key else fail)("remote LLM (optional)", detail,
                          f"add {remote_llm.SECRET_NAME} to the private secrets.env")


def check_model_routing():
    """The owner's routing table (preprocessor/model_routing.py): the model
    each task runs on now, and the local model's state from its breaker
    and last call (no call of its own). WARN when a task set to local has
    no local model to run on or the local model does not answer; FAIL when
    routing.json fails the owner checks, since every task then reads auto."""
    sys.path.insert(0, str(REPO / "preprocessor"))
    try:
        import model_routing
        status, detail, fix = model_routing.doctor_row()
    except Exception as e:
        warn("model routing", f"unreadable: {e}")
        return
    {"PASS": ok, "WARN": warn, "FAIL": fail}[status]("model routing", detail, fix)


def check_push():
    """The optional phone push for installs with no chat platform
    (preprocessor/push_notify.py). Silent when no push.json exists; a
    push.json that fails the owner checks or names a bad URL FAILs. It
    sends nothing: `push_notify.py test` does."""
    sys.path.insert(0, str(REPO / "preprocessor"))
    try:
        import push_notify
    except Exception as e:
        warn("phone push (optional)", f"module unreadable: {e}")
        return
    try:
        settings = push_notify.load_settings()
    except Exception as e:
        fail("phone push (optional)", f"{push_notify.settings_path()} refused: {e}",
             "chmod 600 the file; it must be the owner's, in the private dir")
        return
    if settings is None:
        return
    bad = push_notify.problems(settings)
    if bad:
        fail("phone push (optional)", "; ".join(bad), f"fix {push_notify.settings_path()}")
        return
    extra = push_notify.warnings(settings)
    (warn if extra else ok)("phone push (optional)",
                            "; ".join(extra) or f"topic set at {settings.get('ntfy_url')}",
                            "python3 preprocessor/push_notify.py test sends one push")


# The persistence digest runs daily at 06:40; a day and a half without a
# completed run means its cron line is gone or each run fails.
DIGEST_STALE_HOURS = 36
DIGEST_SCRIPT = "/opt/ea-system/scripts/persistence-digest.py"


def check_persistence_digest(now: float | None = None):
    """The last completed run of scripts/persistence-digest.py, from the
    `checked` time in its owner-private snapshot. No snapshot WARNs (a new
    install before its first run); a run older than DIGEST_STALE_HOURS
    FAILs, because a change to ea-bot's own stores then goes unreported."""
    import time as _time
    from datetime import datetime
    import persistence_digest
    ot = _owner_trust()
    path = _private_dir() / persistence_digest.SNAPSHOT_FILE
    name = "persistence digest"
    fix = (f"the cron line is in docs/SETUP.md 'Cron schedule'; python3 {DIGEST_SCRIPT} runs it "
           "now, and ~/.ea-private/logs/persistence-digest.log says why a run failed")
    try:
        doc = json.loads(ot.read_owner_file(path))
    except FileNotFoundError:
        warn(name, f"no run yet ({path} missing)", fix)
        return
    except (OSError, ValueError, ot.OwnerTrustError) as e:
        fail(name, f"snapshot unreadable: {e}", fix)
        return
    try:
        checked = datetime.fromisoformat(str(doc.get("checked"))).timestamp()
    except (AttributeError, TypeError, ValueError):
        fail(name, "the snapshot has no run time", fix)
        return
    hours = ((now if now is not None else _time.time()) - checked) / 3600
    if hours > DIGEST_STALE_HOURS:
        fail(name, f"last completed run {hours:.0f} h ago (more than {DIGEST_STALE_HOURS} h)", fix)
    else:
        ok(name, f"last completed run {hours:.0f} h ago")


def check_updates(ea_dir: Path) -> None:
    """The updater (scripts/update.py, docs/updates.md): the installed
    version, the channel, the newest release on it, the last apply and any
    version held back. PASS or WARN: an install that does not update still
    works, so this row never fails it."""
    import update_status
    status, detail, fix = update_status.doctor_row(
        update_status.live_dir(), update_status.private_dir(), ea_dir)
    (ok if status == "PASS" else warn)("updates", detail, fix)


# project_dashboard.FALLBACK_FILE, spelled here so the doctor does not
# import the dashboard and its store (a test keeps the two equal).
DASH_FALLBACK_FILE = "dashboard-bind-fallback.json"


def check_dashboard_bind():
    """The dashboard leaves DASH_FALLBACK_FILE in the private dir while it
    serves 127.0.0.1 in place of the address it was asked for, because no
    EA_DASH_TOKEN was set (preprocessor/project_dashboard.py). Silent when
    the file is absent."""
    ot = _owner_trust()
    path = _private_dir() / DASH_FALLBACK_FILE
    try:
        doc = json.loads(ot.read_owner_file(path))
    except FileNotFoundError:
        return
    except (OSError, ValueError, ot.OwnerTrustError) as e:
        doc = {"why": f"fallback file unreadable: {e}"}
    if not isinstance(doc, dict):
        doc = {}
    wanted = doc.get("wanted") or "a non-loopback address"
    warn("dashboard: loopback fallback",
         f"asked for {wanted} with no EA_DASH_TOKEN; serving 127.0.0.1 only"
         + (f" since {doc['since']}" if doc.get("since") else ""),
         "scripts/set-secret.sh EA_DASH_TOKEN (a long random value), then "
         "restart the ea-dashboard service; or EA_DASH_BIND=127.0.0.1 to mean loopback "
         "(docs/SETUP.md 'Project dashboard')")


def check_cron():
    # The owner's copy in the home directory, the file the sentinel compares
    # against (sentinel.CRONTAB_MASTER), not one in the repo.
    master = Path.home() / "crontab.master"
    if not master.exists():
        warn("crontab.master", f"{master} missing (the cron schedule the sentinel compares against)",
             f"crontab -l > {master}")
        return
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        fail("crontab", "no crontab installed", f"crontab {master}")
        return
    want = [ln.strip() for ln in master.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    have = {ln.strip() for ln in r.stdout.splitlines()
            if ln.strip() and not ln.strip().startswith("#")}
    missing = [ln for ln in want if ln not in have]
    extra = sorted(have - set(want))
    detail = ""
    if missing or extra:
        detail = (f"{len(extra)} line(s) only installed, {len(missing)} only in master, "
                  f"e.g. {(extra or missing)[0][:70]}")
    (ok if not detail else fail)("crontab matches crontab.master", detail,
                                 f"if the installed lines are intended, crontab -l > {master}; "
                                 f"if master is, crontab {master}")


# ── main ──────────────────────────────────────────────────────────────

_LISTS = (REQUIRED_CONFIG, OPTIONAL_CONFIG, REQUIRED_ENV, OPTIONAL_ENV, REQUIRED_SECRETS)


def configure(ea_dir: Path, profile: str | None = None, ci: bool = False) -> str:
    """Set the globals the checks read for this install: the profile (the
    argument, else ea-config.json, else assistant), the chat frontend (the
    owner-private chat.json when it passes the checks, else the ea-config.json
    block, which `ci` always uses) and the lists that follow from them.
    Starts from the module defaults each time. Returns the profile."""
    global NEEDS_SLACK, NEEDS_CALENDAR, REQUIRED_CONFIG, OPTIONAL_CONFIG, REQUIRED_ENV, OPTIONAL_ENV, REQUIRED_SECRETS
    global CHAT_FRONTEND, CHAT_CFG
    REQUIRED_CONFIG, OPTIONAL_CONFIG, REQUIRED_ENV, OPTIONAL_ENV, REQUIRED_SECRETS = _LISTS
    if profile is None:
        try:
            import ea_config
            profile = ea_config.get_profile(ea_dir / "config" / "ea-config.json")
        except Exception:
            profile = "assistant"
    NEEDS_SLACK = profile == "assistant"
    try:
        import ea_config
        # ea_config.py has no calendar feature of its own: the Create
        # button is what makes a real event, so `buttons` decides.
        NEEDS_CALENDAR = bool(ea_config.profile_features(profile)["buttons"])
    except Exception:
        NEEDS_CALENDAR = NEEDS_SLACK
    try:
        import ea_config
        # The owner's view: the private chat.json when it passes the checks
        # (what the bot uses), else the ea-config.json block.
        owner_block = None if ci else _trusted_chat_block()
        CHAT_CFG = (ea_config.chat_from_block(owner_block) if owner_block is not None
                    else ea_config.get_chat(ea_dir / "config" / "ea-config.json"))
        CHAT_FRONTEND = str(CHAT_CFG.get("frontend") or "slack")
    except Exception:
        CHAT_CFG, CHAT_FRONTEND = {}, "slack"
    if NEEDS_SLACK and CHAT_FRONTEND != "slack":
        # Another frontend carries the chat: the Slack env, the Slack
        # scheduling channel and the webhooks are not checked at all, and
        # the secrets follow the frontend.
        REQUIRED_CONFIG = tuple(c for c in REQUIRED_CONFIG if c != "slack_webhooks.json")
        OPTIONAL_ENV = tuple(e for e in OPTIONAL_ENV if e != "EA_SCHED_CHANNEL_ID")
        REQUIRED_ENV = ()
        REQUIRED_SECRETS = FRONTEND_SECRETS.get(CHAT_FRONTEND, ())
    if not NEEDS_SLACK:
        # No chat platform: no chat key is asked for, required or optional.
        REQUIRED_CONFIG = tuple(c for c in REQUIRED_CONFIG if c not in SLACK_CONFIG)
        OPTIONAL_CONFIG = OPTIONAL_CONFIG + SLACK_CONFIG
        OPTIONAL_ENV = tuple(e for e in OPTIONAL_ENV if e != "EA_SCHED_CHANNEL_ID")
        REQUIRED_ENV = ()
        REQUIRED_SECRETS = ()
    return profile


def main() -> int:
    ap = argparse.ArgumentParser(description="EA install checklist (read-only)")
    ap.add_argument("--ci", action="store_true", help="templates only; no system or network checks")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--share", action="store_true",
                    help="mask addresses, account names, hosts and ids in the output, for "
                         "pasting into a public issue")
    ap.add_argument("--ea-dir", default=os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    ap.add_argument("--profile", default=None, choices=("email", "review-only", "assistant"),
                    help="install profile to check against (default: ea-config.json, else assistant)")
    ap.add_argument("--probe-claude", action="store_true",
                    help="spend one Haiku call as ea-bot to prove claude -p still runs on the "
                         "subscription login")
    ap.add_argument("--probe-codex", action="store_true",
                    help=f"run `python3 {CODEX_CLI} probe` when that script exists (ok/warn; "
                         "never a FAIL)")
    ap.add_argument("--expect-missing", default="", metavar="NAME,NAME,...",
                    help="check names (or prefixes of them) that may FAIL: exit 0 when every "
                         "FAIL matches one, as an install test with no accounts expects "
                         "(docs/install-testing.md lists the names)")
    a = ap.parse_args()
    ea_dir = Path(a.ea_dir)
    profile = configure(ea_dir, a.profile, a.ci)
    ok("profile", profile)
    if NEEDS_SLACK:
        ok("chat frontend", CHAT_FRONTEND)
    check_repo()
    check_templates()
    if not a.ci:
        check_users_and_dirs(ea_dir)
        check_config(ea_dir)
        check_install_overlay(ea_dir)
        if NEEDS_SLACK:
            check_private(ea_dir)
        env = check_env(ea_dir)
        check_auth(ea_dir)
        check_native_accounts(ea_dir)
        check_outlook_web_path(env)
        check_owner_trust(ea_dir)
        check_resident_trust(ea_dir)
        check_chat(env)
        check_services()
        check_dashboard_bind()
        check_hardening()
        check_decks(ea_dir)
        check_remote_llm()
        check_model_routing()
        if not NEEDS_SLACK:
            check_push()
        check_cron()
        check_persistence_digest()
        check_updates(ea_dir)
        if a.probe_claude:
            check_claude_probe()
        if a.probe_codex:
            check_codex_probe()
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    # An expected miss is a FAIL whose name starts with one of the given
    # names; the exit code counts only the others. A name that matched no
    # FAIL is reported, not an error: on the email profile the EA-Managed
    # calendar is a WARN, and naming it still reads right.
    expected = [n.strip() for n in a.expect_missing.split(",") if n.strip()]
    unexpected = [r for r in fails if not any(r["name"].startswith(n) for n in expected)]
    unmatched = [n for n in expected if not any(r["name"].startswith(n) for r in fails)]
    out: list[str] = []
    if a.json:
        doc = {"results": RESULTS, "fail": len(fails), "warn": len(warns),
               "pass": len(RESULTS) - len(fails) - len(warns)}
        if expected:
            doc["expected_missing"] = expected
            doc["unexpected_fail"] = [r["name"] for r in unexpected]
            doc["expected_missing_unmatched"] = unmatched
        out.append(json.dumps(doc, indent=1))
    else:
        mark = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}
        for r in RESULTS:
            line = f"[{mark[r['status']]}] {r['name']}"
            if r["detail"]:
                line += f"  ({r['detail']})"
            out.append(line)
            if r["status"] != "PASS" and r["fix"]:
                out.append(f"           fix: {r['fix']}")
        out.append(f"\n{len(RESULTS) - len(fails) - len(warns)} ok, {len(warns)} warn, {len(fails)} FAIL"
                   + ("" if a.ci else f"  ({ea_dir})"))
        if expected:
            out.append(f"expected missing: {', '.join(expected)}"
                       + (f"; did not fail: {', '.join(unmatched)}" if unmatched else ""))
            out.append("unexpected FAIL: " + ", ".join(r["name"] for r in unexpected)
                       if unexpected else "no unexpected FAIL")
        if a.share:
            out.append("(--share: addresses, account names, hosts and ids masked)")
    text = "\n".join(out)
    print(share_mask(text) if a.share else text)
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
