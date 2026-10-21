# macOS: the claude binary ea-bot runs

*First written 2026-10-16 on a macOS install during `/setup`. Revised 2026-10-16 with
the v1.2.0 upgrade: ea-bot now has its own Claude Code.*

## The decision

ea-bot runs **its own native install** of Claude Code at
`/Users/ea-bot/.local/bin/claude` (a symlink into
`/Users/ea-bot/.local/share/claude/versions/`). It updates itself. Nothing is
copied from the owner's install. `scripts/install/setup-ea-user-macos.sh` step 6b installs it,
and `scripts/ea-doctor.py` checks it. This is upstream's design (CHANGELOG
v1.2.0, SETUP.md "The service account").

| Path | Owner | What it is |
|---|---|---|
| `/Users/ea-bot/.local/bin/claude` | ea-bot | The binary ea-bot runs. |
| `~/.ea-private/bot.env` | owner 600 | `CLAUDE_BIN=/Users/ea-bot/.local/bin/claude` pins the bot to it. |
| `/Users/ea-bot/.claude/settings.json` | ea-bot 600 | ea-bot's login token (below). No `DISABLE_AUTOUPDATER`. |

## Why, and what it gives up

The owner's install lives under the owner's home, which is closed to ea-bot
on purpose (`docs/SECURITY.md`: a prompt-injected EA is an ea-bot shell), so
ea-bot cannot run it.

The first macOS install instead used a root-owned copy at
`/usr/local/bin/claude`, which a root LaunchDaemon synced from the owner's
version every six hours. It was replaced because:

- **The gain was narrow.** A root-owned binary stops an injected ea-bot
  session from replacing the program it runs. But ea-bot still controls its
  own `~/.claude` (settings, hooks, MCP config), and a hook runs any command
  when claude starts. So an injected session can persist through config with
  either design, and in both its code runs only as ea-bot.
- **It cost a root job and a lasting fork.** A root process read files from
  the owner's home, and every upstream release conflicted in the setup
  script, the doctor and these docs.

What remains true: the binary is ea-bot-writable, so treat ea-bot's whole
`~/.local` and `~/.claude` as attacker-controlled after an injection. The
owner's home stays closed, and nothing runs ea-bot's binary as the owner or
as root.

Rejected earlier, still rejected: ACLs that let ea-bot traverse the owner's
home to `~/.local/bin`. "Search" on the home lets ea-bot open any
world-readable file whose name it can guess (on that install:
`~/.huggingface_token`, `~/.docker/config.json`, `~/.zshrc`, `~/.zshenv`).

## How each caller finds claude

- **The bot** (`ea-chat-bot.py`, `_detect_claude_bin`): `$CLAUDE_BIN` (set in
  `bot.env`), else `/Users/ea-bot/.local/bin/claude` if the owner can see it,
  else `/usr/local/bin/claude`, else `which claude`. The startup log line
  names the choice: `claude_bin=...`.
- **Cron agents** (`run-agent.sh`): `sudo -u ea-bot env PATH="$EA_BOT_HOME/.local/bin:$PATH" bash -c "... claude ..."`.
- **The ea-bot shell** (`scripts/ea-bot-shell.sh`): the same order, tested as
  ea-bot.

## ea-bot's Claude login: a long-lived token, not the keychain

On macOS, Claude Code stores its login in the keychain and does not fall back
to a file. ea-bot is a hidden service account with no login keychain, so
`/login` as ea-bot ends in "Couldn't save your login", and a keychain dialog
offering "Reset To Defaults" appears in the owner's desktop session. **Click
Cancel there**: what a reset would act on from that session is unclear, and
the risk is the owner's own keychain.

A keychain made for ea-bot does not solve it: macOS unlocks a keychain per
security session. Tested 2026-10-16: unlocked from the owner's shell, a test
keychain read fine from three later sudo sessions, and failed from cron's
session with exit 36 (user interaction not allowed).

So ea-bot uses a subscription token instead:

```bash
cd / && sudo -u ea-bot env HOME=/Users/ea-bot /Users/ea-bot/.local/bin/claude setup-token
```

approved in the browser with the EA's subscription account, then stored as
`env.CLAUDE_CODE_OAUTH_TOKEN` in `/Users/ea-bot/.claude/settings.json`
(ea-bot, mode 600). Claude reads that file at every start, so the bot, cron
and `scripts/ea-bot-shell.sh` all get it with no code change.

- **It expires about a year after it is made: renew around 2027-10.** Run the
  command above again and replace the value. `scripts/ea-doctor.py` reports
  the login as `CLAUDE_CODE_OAUTH_TOKEN in .claude/settings.json (renew
  yearly)`; when it lapses, every EA turn fails with an authentication error.
- ea-bot can read the token, as on Linux, where ea-bot owns its
  `.credentials.json`.
- The token covers the model. The claude.ai connectors (Gmail, Google Calendar
  MCP) may not come with it; the EA reads Outlook and the Google calendars
  through its own clients and the mail mediator.
- The `usage` chat command (`preprocessor/claude_usage.py`) reads this token
  when ea-bot has no credentials file. On 2026-10-16
  `https://api.anthropic.com/api/oauth/usage` answered HTTP 429 to it
  repeatedly over 15 minutes, while model calls on the same token worked.
  Treat `usage` as unavailable on this install unless that changes.
- Set it without printing it: copy the token, then pipe `pbpaste` into a
  `sudo -u ea-bot` writer, and clear the clipboard with `pbcopy < /dev/null`.

## sudoers

`/etc/sudoers.d/ea-bot` reads `<owner> ALL=(ea-bot) NOPASSWD: ALL`.

Upstream's script writes `NOPASSWD: <claude path>`. Both callers run
`sudo -u ea-bot env ...` (`ea-chat-bot.py`, `run-agent.sh`), so the command
sudo matches is `/usr/bin/env`, and a claude-only rule refuses them. Tested
on macOS, 2026-10-16: `sudo -n -u ea-bot <claude> --version` worked, and
`sudo -n -u ea-bot env HOME=/Users/ea-bot <claude> --version` answered
`sudo: a password is required`. A rule naming `/usr/bin/env` would be no
narrower, since env runs anything. The rule lets the owner become the
less-privileged ea-bot; it gives ea-bot nothing. Reported upstream.

A test note: `claude` run as ea-bot from inside the owner's home fails with
`error: An unknown error occurred (Unexpected)`, because ea-bot cannot access
the working directory. Test from `/` or `/tmp`.

## Troubleshooting

**Which binary does the bot run?** `grep claude_bin= ~/.ea-private/logs/org.ea-system.ea-chat-bot.launchd.log | tail -1`.

**Is ea-bot's install current?**
```bash
cd / && sudo -u ea-bot env HOME=/Users/ea-bot /Users/ea-bot/.local/bin/claude --version
cd / && sudo -u ea-bot env HOME=/Users/ea-bot /Users/ea-bot/.local/bin/claude update
```

**It is missing.** Reinstall as ea-bot:
`sudo -u ea-bot env HOME=/Users/ea-bot bash -c 'curl -fsSL https://claude.ai/install.sh | bash'`.

**The EA fails with `sudo: a password is required`.** The sudoers rule no
longer matches what the caller runs, typically because a re-run of an
upstream setup script wrote the claude-only rule back. Check with
`sudo -l -U <owner>`; it must list `(ea-bot) NOPASSWD: ALL`.
