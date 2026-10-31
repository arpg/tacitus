# macOS Compatibility

*First install: a second user's Mac, 2026-04-07/08. Autodetect refactor: 2026-04-20.*

The EA system was built for Linux (Ubuntu/Debian). This document records the
friction points discovered during the first macOS install and what was done about
each one.

A Mac can also skip the Graph and Google app registrations and use the
accounts in System Settings > Internet Accounts, through EventKit and
Mail.app: [macos-native-accounts.md](macos-native-accounts.md) (a design
with a prototype; not yet run on a Mac).

---

## What the code now handles automatically

These items previously required manual source edits. They are now resolved at
runtime via auto-detection.

### 1. Claude binary location (`ea-chat-bot.py`)

`CLAUDE_BIN` is found by searching the `CLAUDE_BIN` env var → ea-bot's own
`<ea-bot home>/.local/bin/claude` → `/usr/local/bin/claude` → `/usr/bin/claude`
→ `which claude`. No hardcoded path. The bot launches Claude as ea-bot, so the
binary has to be one ea-bot can execute: your own install (under your home,
which stays private) is not, and the setup scripts give ea-bot its own native
install in its own home, which updates itself. Never share or sync your copy
into ea-bot's space.

### 2. Process isolation (`ea-chat-bot.py`, `run-agent.sh`)

Isolation runs Claude subprocesses as a separate `ea-bot` Unix user so
Claude cannot touch source code, config, or auth tokens. It is
**mandatory** — Layer 1 of the security model (see
`docs/SECURITY.md`). Both `ea-chat-bot.py` and `run-agent.sh` refuse
to start if the `ea-bot` user is missing, and there is no
`EA_ISOLATION_MODE` opt-out. An earlier iteration of the code
auto-disabled isolation on Darwin; that behaviour has been removed
because any fail-open path silently invalidates the security model.

macOS supports isolation identically to Linux — Darwin is a full Unix
and `sudo -u ea-bot` behaves the same way. The only platform-specific
differences are:

- The ea-bot user's home typically lives under `/Users/` (macOS) rather
  than `/home/` (Linux). The code looks the path up via
  `pwd.getpwnam`, so no path is hardcoded.
- User management uses `dscl`/`sysadminctl` rather than `useradd`.

**Setup:** run the macOS-specific provisioning script once:
```bash
sudo bash scripts/install/setup-ea-user-macos.sh
```

It creates a hidden `ea-bot` service account (UID 499, not on the
login window), deploys the source tree to `/Users/ea-bot/ea-system/`,
sets the same permission model as the Linux script (source read-only,
state read-write, config read-only), and installs the sudoers entry
(`%invoker% ALL=(ea-bot) NOPASSWD: /path/to/claude`). Re-runnable /
idempotent. See the script header for the exact steps.

**Manual fallback** if the script hits issues on your macOS version:
```bash
sudo dscl . -create /Users/ea-bot
sudo dscl . -create /Users/ea-bot UserShell /usr/bin/false
sudo dscl . -create /Users/ea-bot RealName "EA Bot"
sudo dscl . -create /Users/ea-bot UniqueID 499
sudo dscl . -create /Users/ea-bot PrimaryGroupID 499
sudo dscl . -create /Users/ea-bot NFSHomeDirectory /Users/ea-bot
sudo dscl . -create /Users/ea-bot IsHidden 1
sudo dscl . -passwd /Users/ea-bot '*'
sudo mkdir -p /Users/ea-bot && sudo chown ea-bot /Users/ea-bot

# ea-bot's own Claude Code (never your copy under your private home)
sudo -u ea-bot env HOME=/Users/ea-bot bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
echo "$(whoami) ALL=(ea-bot) NOPASSWD: /Users/ea-bot/.local/bin/claude" | \
    sudo tee /etc/sudoers.d/ea-bot
sudo chmod 440 /etc/sudoers.d/ea-bot
sudo visudo -cf /etc/sudoers.d/ea-bot   # syntax check
```
Then start the bot and verify the startup log contains
`isolation: ENFORCED user=ea-bot home=/Users/ea-bot ...`.

### 3. Venv PATH for Claude subprocesses (`ea-chat-bot.py`)

Homebrew Python enforces PEP 668 — packages must live in a venv.
`ea-chat-bot.py` now injects `.venv/bin` into PATH before spawning Claude
subprocesses so `python3 preprocessor/...` commands resolve to venv Python.

### 4. Portable symlink resolution (`run-agent.sh`)

`readlink -f` is Linux-only. `run-agent.sh` now uses `python3 -c os.path.realpath`
(with a `cd && pwd -P` fallback) to resolve `EA_DIR` on both platforms.

### 5. Venv activation in `run-agent.sh`

If `.venv/bin/python3` exists in the script directory, that bin directory is
prepended to PATH and all `python3` invocations use `$PYTHON3` (the venv
binary). No manual venv activation needed before running the script.

### 6. Timezone detection (`scripts/install/user_init.py`)

`detect_system_timezone()` now tries `systemsetup -gettimezone` on Darwin
before falling back to `timedatectl` and `/etc/timezone`.

---

## What still requires manual setup on macOS

### A. Create the venv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install playwright && playwright install chromium
```

### B. Order: config templates before Google auth

Copy the config templates (`scripts/install/setup.sh`) BEFORE running Google Calendar auth
(SETUP.md "Google OAuth app").
`google_calendar_client.py --setup` reads `~/.ea/config/scheduling-preferences.json`
and fails if it doesn't exist. Populate the `calendars` section with the user's
Google accounts before running `--setup`.

### C. Google Cloud OAuth: add test users

Google Cloud projects in "Testing" mode require each Google account to be added
as a test user before it can complete the OAuth flow:
APIs & Services → OAuth consent screen → Test users → Add users

### D. Skip `scripts/install/setup-ea-user.sh` (Linux-only)

`scripts/install/setup-ea-user.sh` uses `useradd`/`groupadd`/`usermod` — none of which exist on
macOS. Skip the Linux script. If you want isolation on macOS (recommended for any
shared/production host), create the ea-bot user manually via `dscl` as
shown in section 2 above. For single-user dev installs the `auto` default
will detect no ea-bot user and run without isolation.

### E. Daemon: launchd instead of systemd

Skip the systemd unit (SETUP.md "Services"). Create a launchd plist at
`~/Library/LaunchAgents/com.ea-system.slack-bot.plist` instead.

Critical plist requirements:
- `ProgramArguments`: use `.venv/bin/python3` as the interpreter
- `EnvironmentVariables` must include all vars from `~/.ea/.env`
  (`EA_SLACK_CHANNEL_ID`, `EA_SLACK_CHANNEL_NAME`, `EA_SLACK_OWNER_ID`, `EA_MODEL`).
  Launchd has no equivalent of systemd's `EnvironmentFile`.
- `PATH` must include `.venv/bin`
- Use `launchctl load/unload` to start/stop

### F. Slack app: Event Subscriptions

SETUP.md "The Slack app" lists this step; it was missing once. After enabling Socket Mode and
Interactivity:
1. Event Subscriptions → Enable Events → On
2. Subscribe to bot events: `message.channels` and `message.groups`
3. Save (reinstall app if prompted)

Without this, the bot connects but receives zero message events. Symptom: logs
show "Bolt app is running" but no "RAW message event" lines appear.

---

## Setup checklist (macOS)

```
[ ] python3 -m venv .venv && pip install -r requirements.txt
[ ] playwright install chromium
[ ] Config templates copied BEFORE Google Calendar auth
[ ] scheduling-preferences.json populated with Google accounts
[ ] Google test users added in Cloud Console
[ ] google_calendar_client.py --setup completed for all accounts
[ ] google_calendar_client.py --test shows events
[ ] Graph API auth working (graph_client.py sign-in, SETUP.md "Microsoft Graph app registration",
    or an owner plugin's token source: docs/plugins.md)
[ ] Slack app has Event Subscriptions enabled (message.channels, message.groups)
[ ] ea-bot user created via `sudo bash scripts/install/setup-ea-user-macos.sh`
    (MANDATORY — bot refuses to start without it; see §2)
[ ] Startup log shows `isolation: ENFORCED user=ea-bot home=/Users/ea-bot`
[ ] launchd plist created with all env vars and venv PATH
[ ] Bot responds to test message in Slack
[ ] Cron entries use absolute venv Python path or PATH=.venv/bin:$PATH prefix
[ ] MCP connectors added (slack, gmail, gcal)
```
