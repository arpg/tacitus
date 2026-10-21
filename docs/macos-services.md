# macOS: the EA's services under launchd

*Written 2026-10-16 during a macOS `/setup`.
Companion to [macos-claude-binary.md](macos-claude-binary.md).*

## Why this exists

The `assistant` profile runs two long-lived services as the **owner** (not
ea-bot): the chat bot and the mail mediator. On Linux they are systemd units,
hardened by `deploy/harden-permissions.sh` (SETUP.md "Hardening"). Neither
systemd nor that script exists on macOS, so they are ported to launchd here.
The port keeps the hardened behaviour, not the mechanism:

| Guarantee (Linux, after 9b) | macOS |
|---|---|
| Code runs from `/opt/ea-system`, an owner-owned export under root-owned `/opt`, never from ea-bot's clone | same path, same `git archive` export |
| Interpreter from the owner's private home | `~/ea-system/.venv/bin/python3` |
| Environment only from owner-private files (`EnvironmentFile=` of `~/.ea-private/bot.env`, `secrets.env`) | the root-owned launcher `/usr/local/libexec/ea-service-run.sh` parses those two files |
| Unit files ea-bot cannot edit (`/etc/systemd/system`) | root-owned plists in `/Library/LaunchDaemons` |
| `SupplementaryGroups=ea` | `UserName` + `InitGroups`: launchd reads the owner's current groups at each start, so no re-login is needed after joining `ea` |
| Deploy hook restarts via a narrow sudoers grant | the same, for two `launchctl kickstart` commands |

## What is installed

`sudo bash deploy/macos/install-services.sh` (after the chat platform is
configured; see "Order" below). Re-runnable. `--status` reports, `--remove`
undoes (it leaves `/opt/ea-system` and `~/.ea-private`).

| Path | Owner, mode | Role |
|---|---|---|
| `/Library/LaunchDaemons/org.ea-system.ea-chat-bot.plist` | root:wheel 644 | runs `/opt/ea-system/ea-chat-bot.py` as the owner |
| `/Library/LaunchDaemons/org.ea-system.ea-mail-mediator.plist` | root:wheel 644 | runs `/opt/ea-system/preprocessor/mail_mediator.py` as the owner, with `EA_DIR` set |
| `/usr/local/libexec/ea-service-run.sh` | root:wheel 755 | loads the env files, then execs the service |
| `/opt/ea-system/` | owner:ea 750 | the code both services run; `.deployed-commit` names its commit |
| `~/.ea-private/bot.env` | owner 600 | `CLAUDE_BIN=/Users/ea-bot/.local/bin/claude`, `EA_MODEL` |
| `~/.ea-private/secrets.env` | owner 600 | chat token, API secrets (you write it) |
| `~/.ea-private/chat.json` | owner 600 | the chat server, bot and owner id the bot trusts (seeded once) |
| `~/.ea-private/logs/<label>.launchd.log` | owner 600 | each service's stdout and stderr |
| `/etc/sudoers.d/ea-services` | root 440 | owner may run `launchctl kickstart -k` for these two labels only |

The labels start with `org.ea-system` on a new install. An install that
was deployed under the legacy prefix keeps its labels: the installers and
`preprocessor/launchd_labels.py` use whichever plist is installed. Read
`org.ea-system` in the commands below as that prefix.

Both plists set `KeepAlive` (restart on exit; throttled to 10 s for the bot,
5 s for the mediator) and `RunAtLoad` (start at boot, no login needed).

The launcher refuses to start a service (exit 78, logged) if an env file, or
the directory it is in, is not owned by the owner or is group- or
world-writable, or is a symlink. It reads `KEY=VALUE` lines only and never
evaluates them as shell.

## Order

1. `sudo bash scripts/install/setup-ea-user-macos.sh` (the service account and its own
   Claude Code).
2. On `assistant`, the chat platform (SETUP.md "Zulip server", or "Slack")
   and its secret in `~/.ea-private/secrets.env`. On `email` and
   `review-only` there is none.
3. `sudo bash deploy/macos/install-services.sh`, on every profile. It
   installs the mediator job always and the bot job only when the profile
   has a chat platform (a bot job from an earlier profile is removed).
   Step 3 of it seeds
   `chat.json` from `ea-config.json`'s chat block **only when the file is
   missing**. Run before step 2, it would freeze the template values, and the
   bot would keep reading them. To re-seed after a chat change: delete
   `~/.ea-private/chat.json` and re-run the installer.
4. `sudo bash deploy/enable-mail-mediation.sh` (macOS-aware): turns the flag
   on, self-tests the ea-bot → mediator path, then makes the tokens
   owner-only.

## Deploys

`.githooks/post-commit` (active once `git config core.hooksPath .githooks`),
on a commit to `main` in the main checkout, hands off on macOS to
`deploy/macos/deploy.sh`. That script can also be run by hand, as the owner:

1. fetches HEAD into ea-bot's clone (`/Users/ea-bot/ea-system`), after
   checking that directory is real, owner-owned and not writable by others;
2. mirrors HEAD into `/opt/ea-system` with `git archive`;
3. if bot code changed, waits up to 1200 s for in-flight turns
   (`~/.ea-private/bot-inflight.json`), then `sudo -n launchctl kickstart -k`;
4. if mediation code changed, restarts the mediator the same way.

Commits on another branch (such as an install branch) do not deploy.
Run `bash deploy/macos/deploy.sh` to deploy them.

## Python environments

Three interpreters are in play, and each must have `requirements.txt`:

| Who | Interpreter | Made by |
|---|---|---|
| the services (owner) | `~/ea-system/.venv` (Python 3.14; `ortools` has no 3.15 wheel yet) | `python3.14 -m venv .venv` in the repo |
| ea-bot's Claude and its tool calls | `/Users/ea-bot/ea-system/.venv` (the bot puts it first on PATH, `ea-chat-bot.py:524`) | the owner, in the deployed clone, which the owner owns |
| cron (`/opt/ea-system/run-agent.sh` and bare `python3` lines) | the owner's venv, through a `PATH=` line at the top of the crontab | `scripts/install/setup.sh` / SETUP.md "The venv on the cron PATH" |

`/opt/ea-system/.venv` never exists: every deploy mirrors git, and the venv is
not in git.

## Troubleshooting

**State of everything:** `sudo bash deploy/macos/install-services.sh --status`

**A service is not running.**
```bash
sudo launchctl print system/org.ea-system.ea-chat-bot | grep -E 'state|pid|last exit'
tail -30 ~/.ea-private/logs/org.ea-system.ea-chat-bot.launchd.log
```
- `last exit code = 78` and an `ea-service-run:` line: an env file or
  `~/.ea-private` has the wrong owner or mode. Fix with `chmod 600` /
  `chmod 700`; do not loosen the launcher.
- A Python traceback: the service's own error. For the bot, the usual causes
  are a chat platform not configured, or `chat.json` seeded with template
  values (re-seed as in "Order").
- Restarting every 10 s: `KeepAlive` is restarting a crash. The log has it.

**Restart by hand:** `sudo launchctl kickstart -k system/org.ea-system.ea-chat-bot`
**Stop until next boot:** `sudo launchctl bootout system/org.ea-system.ea-chat-bot`
**Start again:** `sudo launchctl bootstrap system /Library/LaunchDaemons/org.ea-system.ea-chat-bot.plist`

**The bot runs old code.** Compare `cat /opt/ea-system/.deployed-commit` with
`git rev-parse --short HEAD`. If they differ, `bash deploy/macos/deploy.sh`. If
they match, restart the bot.

**The deploy says it could not restart.** `/etc/sudoers.d/ea-services` is
missing or names other labels. Re-run `install-services.sh`. Check with
`sudo -l`.

**The deploy says UNSAFE DEPLOY TREE.** `/Users/ea-bot/ea-system` or its
`.git` is not owned by you, or is group- or world-writable, or is a symlink.
That is the check working: something changed the tree. Look before fixing;
`sudo bash scripts/install/setup-ea-user-macos.sh` re-creates a safe copy if you move the bad
one aside.

**ea-bot's tool calls fail with `ModuleNotFoundError`.** ea-bot's venv is
missing or stale: rebuild `/Users/ea-bot/ea-system/.venv` with
`python3.14 -m venv` and `pip install -r requirements.txt`, as the owner.

**After a macOS upgrade.** Plists in `/Library/LaunchDaemons` survive. If
System Settings > General > Login Items & Extensions lists them under
"Allow in the Background" as off, turn them on.

## Restarts, FileVault and the Zulip server

With FileVault on, after an unplanned reboot (power loss, crash) the Mac
waits at the FileVault password screen and **nothing runs**: no
LaunchDaemon, no cron, no Colima. That is FileVault working; keep it on, since
this machine holds the mail tokens.

- Planned restarts (macOS updates): `sudo fdesetup authrestart` unlocks once
  and comes back unattended.
- Unlocking FileVault logs the owner in, which starts Colima through the
  Homebrew login item `sh.brew.colima` (`RunAtLoad`; `brew services list`
  shows it "stopped" because `colima start` exits once the VM is up). The
  five Zulip containers use `restart: unless-stopped`, so they follow Docker.
- The chat bot's `KeepAlive` retries until Zulip answers.

Zulip recovery is the owner's `~/zulip-ctl.sh`, outside this repo:
`check` (diagnose only), `heal` (default: repair Tailscale, Colima, Docker,
the containers, the `tailscale serve` route), `restart`, `backup` (to
`~/zulip-backups`; copy off the machine too) and `update [TAG]`.
