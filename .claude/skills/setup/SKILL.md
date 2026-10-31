---
name: setup
description: Guided install of the EA system, the front door for a new user. Offers the browser setup page first, or asks here: the profile first, then the account questions one screen at a time, writes the config, runs the service-account and cron installers, ends with the doctor's checklist green, and asks the first three preference questions.
---

You are installing the EA system for the person in front of you. Work
from this repository as the invoking user; never as `ea-bot`. This skill
is the whole install: every question and every command is here.
`docs/SETUP.md` is the reference; when a step needs background (a portal's
screens, a file's keys, why a rule is shaped as it is) this skill names
the docs/SETUP.md appendix to read, by heading. Ask one question at a time
with the AskUserQuestion tool. Write each answer down before the next
question. Never guess a value the user has not given you.

## Read first: the invariants

You are a code-editing agent with write access to this repository. These
invariants come from `docs/SECURITY.md` and are not negotiable. If a
step fails, debug the cause; never "simplify around" it by weakening a
security check.

1. **ea-bot isolation is mandatory.** Do not remove, comment out, or add
   an opt-out to the startup FATAL in `ea-chat-bot.py` or `run-agent.sh`.
   If the bot refuses to start because the ea-bot user is missing, run
   the service-account installer (section 5); do not patch the check.
2. **Source code is read-only for ea-bot.** Do not `chmod g+w` `.py`
   files to resolve a Write-tool "Permission denied". That error is
   Layer 1 working as designed. Edit as the invoking user in the dev
   repo, never as ea-bot and never by relaxing the deployed copy.
3. **`~/.ea-private/secrets.env` is owner-private** (`chmod 600`, in a
   `0700` directory in the user's home). The chat credential and API
   secrets live there; ea-bot must not be able to read it. Do not loosen
   the mode or move a secret to a group-readable file to fix a read
   error; the read error is correct.
4. **No `EA_ISOLATION_MODE` env var, ever.** An earlier iteration had
   `auto`/`user`/`none` modes; any fail-open path invalidates Layer 1.
   Do not reintroduce it under any name, and do not suggest it.
5. **No platform-conditional isolation bypass.** `sudo -u ea-bot` works
   the same on Linux and macOS. If a Darwin-specific path fails (dscl,
   sudoers, Homebrew PATH), fix that path; never add an
   `if Darwin: skip` branch around the isolation call.

When in doubt, stop and ask the user. A ten-minute pause beats a silent
regression of the security model.

## How the commands run

Your Bash tool has no terminal: a command that reads an answer from the
keyboard fails there (Python's `input()` ends in EOFError). So:

- **Questions.** AskUserQuestion is multiple choice. For a name, an
  address, an id or a link, offer the likely values you know (the domain
  of the work email, the detected timezone) and tell the user to pick
  "Other" and type the value.
- **Answers files.** `scripts/install/user_init.py` never asks you anything: write the
  answers to `~/.ea-private/answers-<phase>.json` (mode 600;
  `mkdir -m 700 -p ~/.ea-private` first), then run
  `python3 scripts/install/user_init.py --answers ~/.ea-private/answers-<phase>.json --phase <phase>`.
  The keys are in docs/install-testing.md; `tests/install/answers-email.json`
  is a complete example. Delete a file that holds a secret once its phase
  has run.
- **`scripts/install/setup.sh`** sees no terminal and runs non-interactive by itself: it
  never signs in and never asks. It prints the sign-in command for the
  user instead, and installs the cron lines only with `--install-cron`.
- **Steps the user types.** Some steps need the user's own password,
  browser or login. A line typed in this prompt with a `!` in front shows
  its output here as it runs, but has no terminal and cannot read a key.
  The Graph sign-in only prints a link and waits, so the user types it
  here with a `!`. The `sudo` steps (two on Linux, three on macOS), the
  Google sign-in (a pasted address) and the ea-bot Claude login (a
  full-screen program) read the keyboard and fail with a `!`
  (`sudo: A terminal is required to authenticate`): ask for a second
  terminal in this directory, keep it for the install, and run them
  there. Give the exact line, wait for the user to say it is done, run
  the read-only check named with it, and go on only when it passes.
- **The browser page, offered first.** Ask whether they would rather
  answer on a setup page in a browser (recommended) or here. For the
  page, run `python3 scripts/install/setup_web.py` with
  `run_in_background` (`--bind <tailscale ip -4>` for a phone or laptop
  on their tailnet) and give the user the printed address with its `#t=`
  token, to them only. It asks sections 1, 2, 2b, 3's app values, 4's
  calendars, 6's platform and the push, writes the same files and signs
  in to nothing (docs/setup-web.md). After they press Done, run section
  0 and go on.

## 0. Resume

Before the first question, read what an earlier run left behind and
skip every question it answered. Each section writes a value this list checks:

- `~/.ea/config/ea-config.json` has `"profile"` and `"acknowledged"`,
  and `list-digests.json` is beside it (`setup.sh` ran): section 1 is
  done. With the second missing, its notice is still owed; with only the
  third missing (the browser page records the two), only its `setup.sh` run.
- `~/.ea/config/user-profile.json` has a real `name` and `email` (no
  placeholder): section 2 is done; its `institution` block has
  `domains` and `video.platform`: 2b is done.
- `~/.ea/auth/msal_token_cache.json` exists, or
  `python3 preprocessor/graph_actions.py list-mail --folder inbox --top 1`
  answers: section 3 is done.
- `~/.ea/config/scheduling-preferences.json` names an
  `ea_managed_calendar.calendar_id`, or the user said they never use
  Google: section 4 is done.
- `id ea-bot` answers and `/home/ea-bot/ea-system/CLAUDE.local.md`
  exists: section 5 is done.
- `python3 scripts/ea-doctor.py --profile <name>` prints no FAIL:
  only sections 8 and 9 remain.
- `python3 preprocessor/onboarding.py ask --count 3` prints nothing
  new: section 9 is done.

Tell the user in one line which sections you found done, then start at
the first one that is not.

## 1. Profile

This is the first question, before any other. Ask which profile they
want, and recommend `email` for a first install: no chat, about an
hour once IT has given them the Outlook app. `assistant` is the upgrade
path: with the web chat a few steps more, with Zulip about half a day,
most of it the chat server. The three:

- `email` (recommended first): the EA reads Outlook, keeps the ledger,
  drafts replies in the user's voice and proposes meeting times in its
  report; no holds, no events, no chat platform. The user works with it
  in the Claude Code shell and sends every draft from Outlook. Briefings
  land in `~/.ea/state/reports/<agent>-latest.md`, by phone push when set up.
- `review-only`: `email` without drafting; nothing is written to the mailbox.
- `assistant`: everything, plus the chat platform (the web chat, Zulip
  or Slack): the EA channel, the direct message, project and session
  rooms, the Send and Create buttons, the resident session, plugins, and
  on Zulip and Slack a scheduling channel.

Then show the notice: print the "Your responsibility" paragraph of
`README.md` word for word and ask whether they have read it and accept
that what the EA sends and files under their name is theirs. On
anything but a yes, stop; `/setup` resumes when they are ready. Never
shorten the paragraph or answer for them. On a yes, run
`./scripts/install/setup.sh --profile <name> --acknowledged`. It installs the Python packages,
copies every config template into `~/.ea/config/` (a file already there
is kept), records the profile and the acknowledgment date in
`ea-config.json`, and saves the cron lines without installing them. Run it now, while `~/.ea` is still the
user's own directory, and never write `ea-config.json` by hand before
it: the template would be skipped. To move from `email` to `assistant`
later, run `./scripts/install/setup.sh --profile assistant` and do section 6.
`python3 preprocessor/ea_config.py features --profile <name>` prints
what the profile turns on; use it to decide which sections below apply.
The table of profiles is docs/SETUP.md "Choose a profile".

## 2. Identity (all profiles)

Ask, one at a time: full name, work email, and role and department (one
sentence, the way they would introduce themselves). Each is an "Other"
answer. Then the home timezone: offer the machine's zone (`timedatectl
show -p Timezone --value`, or `readlink /etc/localtime` on macOS) and
"Other". Working hours are asked once, in section 4, where they are
used. One `identity` phase writes sections 2 and 2b together, after 2b.
The keys of every config file are in docs/SETUP.md "Config files".

## 2b. Institution (all profiles)

CLAUDE.md names no institution; the `institution` block of
`user-profile.json` carries it (`preprocessor/institution.py` documents
every key). Ask these one at a time:

1. The institution's name.
2. The mail domains that count as internal. Offer the domain of their
   work email as the first one, and ask whether the institution uses
   others.
3. The safety-rail domain: the EA never sends a first email to an
   address outside it on its own. It is the work email's domain unless
   the user says otherwise: ask only to confirm it.
4. The video-meeting platform they use (Zoom, Google Meet, or another).
5. Their personal meeting link on that platform.

Write sections 2 and 2b to `answers-identity.json` (`full-name`,
`work-email`, `role-description`, `department`, `university`,
`institution-name`, `institution-domains`,
`institution-safety-rail-domain`, `video-platform`, `video-link`) and
run the `identity` phase. It writes the block as `name`, `domains`,
`safety_rail_domain`, `video.platform` and `video.link`, and the same
link as the top-level `zoom_link`, which the Create button reads when it
adds the meeting link to an event. Then write `timezone` to
`answers-scheduling.json` and run the `scheduling` phase. Check:
`python3 tests/validate_config.py` names no placeholder in
`user-profile.json`. Leave `related_domains`, `boilerplate_tokens`,
`boilerplate_phrases` and `letterhead` as the template has them unless
the user names values. For an install at the worked example's
institution, `config/examples/cu-boulder/user-profile.json` holds a
complete block to start from.

The overlay render and install at the end of section 5 carry these
facts into every Claude session. They need the service account, so they
wait until then.

## 3. Outlook (all profiles)

Ask whether they already have an app registration for the EA in their
organization's Microsoft 365 tenant. Most people do not, and most cannot
create one themselves. Choose by the answer:

- Yes: ask for the application (client) id, the tenant id and the client
  secret ("Other" answers), and whether the tenant allows public client
  flows (then `device_code` needs no secret). Write `answers-auth.json`:
  `graph-auth-method` (`confidential` or `device_code`),
  `graph-client-id`, `graph-tenant-id`, `graph-client-secret`
  (confidential only), `"graph-sign-in-now": false` and
  `"google-calendar-now": false`. Run the `auth` phase (the `GRAPH_*`
  keys go to `~/.ea/.env`, the secret to `~/.ea-private/secrets.env`),
  then delete the file. Then the Graph sign-in, a step the user types:
  `! python3 preprocessor/graph_client.py --align --write` for
  `confidential` (it prints a link, waits for the browser's redirect on
  `localhost:8400` and aligns the scopes to what the app grants), or
  `! python3 preprocessor/graph_client.py` for `device_code` (a link and
  a code for any browser). Both flows and the registration's values are
  docs/SETUP.md "Microsoft Graph app registration".
- No: two paths (AskUserQuestion). **Ask IT**, the lasting one: fill in
  `docs/it-request.md` with them, have them send it, stop and tell them to
  re-run `/setup` when IT answers (section 0 resumes here). **IT will not
  register one**: say what docs/SETUP.md "Option C: no registration
  (outlook-web-auth)" does, from its first paragraph, and ask whether
  their organization allows it; unless it is a clear yes, ask IT. On a
  yes, run its four steps; they type the install and `login` with `!`.

Check before moving on:
`python3 preprocessor/graph_actions.py calendar-view --days 1`.

## 4. Google Calendar (all profiles, recommended)

Ask whether they use Google Calendar or Gmail. Skip this section only if
they say they never use Google. Otherwise walk them through docs/SETUP.md
"Google OAuth app": a Google Cloud project, the Calendar API (and Gmail
API when the EA drafts there), the consent screen **published** ("In
production": in "Testing" status Google ends the refresh tokens after 7
days), a Desktop OAuth client, its JSON saved as
`~/.ea/auth/google_client_secret.json` (mode 600). Ask for each Google
account to read ("Other"), add them as `google-calendars` beside
`timezone` in `answers-scheduling.json`, and run the `scheduling` phase
again. Then the Google sign-in for all of them, a step the user types in
the second terminal (it asks them to paste the address the browser lands
on): `python3 preprocessor/google_calendar_client.py --setup`.
Check: `python3 preprocessor/google_calendar_client.py --test`.

Then the EA-Managed calendar (docs/SETUP.md "The EA-Managed calendar"): ask
them to create a secondary Google calendar named `EA-Managed`, paste its
id into `ea_managed_calendar` of `scheduling-preferences.json`, and run
`python3 preprocessor/owner_trust.py seed --calendar` as the user: the
mediator allows button-free writes only to the id in the owner-private
`~/.ea-private/calendars.json`, never to one read from ea-bot's config.
Ask for their working hours (the one time this install asks) and have
them add one recurring `wh Working Hours` event there; without it the
solver has no rule store and no place to hold time. It is required on
`assistant` and optional elsewhere.

## 5. Service account and permissions (all profiles)

`./scripts/install/setup.sh --profile <name>` ran in section 1; run it again only if the
profile changed. Then the service account, a step the user types in the
second terminal: `sudo ./scripts/install/setup-ea-user.sh` (Linux) or
`sudo ./scripts/install/setup-ea-user-macos.sh`.
It creates `ea-bot`, moves `~/.ea` into ea-bot's home, installs ea-bot's
own Claude Code and the sudoers rule, and on Linux turns mail mediation
on. Check: `id ea-bot && test -L ~/.ea && sudo -n -u ea-bot true`.
After this script `~/.ea/config` belongs to `ea-bot`, and a later
`./scripts/install/setup.sh` run writes it as `ea-bot` through that sudoers rule. What
each script does, and the sudoers rule, are docs/SETUP.md "The service
account". Do not weaken a permission to get past an error.

Then ea-bot's Claude login, a step the user types in the second
terminal (a full-screen program):
`sudo -u ea-bot env HOME=/home/ea-bot claude`, then `/login`, then
Ctrl+C. Check:
`sudo -n -u ea-bot test -e /home/ea-bot/.claude/.credentials.json`.
On macOS ea-bot has no keychain: the step is
`cd / && sudo -u ea-bot env HOME=/Users/ea-bot /Users/ea-bot/.local/bin/claude setup-token`
and the token goes in ea-bot's settings (docs/macos-claude-binary.md);
the doctor's `ea-bot: Claude login` line is the check.
On a Linux desktop session, `newgrp ea` (or a new login) gives their
shell the `ea` group.

Then the install overlay (docs/SETUP.md "The install overlay"): `render`, as
`ea-bot`, turns the config into `~/.ea/config/CLAUDE.install.md`;
`install`, as the invoking user who owns the deployed tree, copies it
there as `CLAUDE.local.md`, which every Claude session loads. On Linux:

```bash
sudo -u ea-bot /usr/bin/python3 /home/ea-bot/ea-system/preprocessor/install_overlay.py render
python3 preprocessor/install_overlay.py install --tree /home/ea-bot/ea-system
```

You run both: the sudoers rule lets the invoking user run any command
as `ea-bot` without a password. On macOS, use `/Users/ea-bot` and the
Homebrew Python (`/opt/homebrew/bin/python3`; Apple's is 3.9); the other
differences are docs/SETUP.md "macOS notes". The doctor's two `install
overlay` lines confirm both steps. Run both again after any later change
to `user-profile.json`.

## 6. Chat platform (assistant profile only)

Ask once which chat platform they want, and recommend the web chat:
the bot serves it itself, as an app on their phone over their tailnet,
with no chat server, and every message, file and card stays on this
machine. Zulip suits someone who already runs it. Slack is acceptable
when they accept that everything the EA shows them, drafts and quoted
mail included, is stored by Slack. The comparison is the "Choosing a
chat platform" table in docs/INSTALL.md. The answer goes in
`answers-chat.json` as `"chat-platform"` (`web`, `zulip` or `slack`),
and the `chat` phase records it in the chat block.

For the web chat, docs/SETUP.md "Web chat": Tailscale on this machine
and their phone. Offer this machine's tailnet name (`Self.DNSName` in
`tailscale status --json`, without the last dot) and their login
(`tailscale whois <this machine's tailnet IP>`), each with "Other", as
`web-chat-host` and `web-chat-login` in the same file, then run the
phase. It writes the `web` block: port 8470, base path `/chat`,
`public_url` `https://<name>:8444/chat`, `tailscale_login`. Then a step the user
types in the second terminal:
`sudo tailscale serve --bg --https=8444 --set-path /chat http://127.0.0.1:8470/chat`.
Check: `tailscale serve status` lists `/chat`. The hardening step in
section 8 has them type each web value back as it copies the block to
`~/.ea-private/chat.json`; the doctor's `web chat:` rows then pass.

For Zulip or Slack, run the phase first. For Zulip, docs/SETUP.md
"Zulip server": the docker-zulip stack in `deploy/zulip/README.md`; then
`python3 scripts/zulip_bootstrap.py --site <site> --owner-email <you> --owner-key <key>`,
which prints the `chat` block (with `"frontend": "zulip"`) to paste into
`ea-config.json` and the `ZULIP_API_KEY=` line for
`~/.ea-private/secrets.env` (0600, in a 0700 `~/.ea-private`). Ask for
the owner's Zulip user id if the printed block lacks it. The bot starts
in section 8; confirm its startup line
`frontend=zulip stream=ea (private, subscribed) owner=<id>` there.

For Slack, docs/SETUP.md "Slack" end to end: the app and its scopes, its
icon (`assets/tacitus-avatar.png`, uploaded by hand), Socket Mode, the
event subscriptions, the app-level token, the private channel.
Ask for the bot token, the app token, the channel id, the channel name
and the user's member id, and write `EA_SLACK_CHANNEL_ID`,
`EA_SLACK_CHANNEL_NAME` and `EA_SLACK_OWNER_ID` to `~/.ea/.env` (0640)
and `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` to `~/.ea-private/secrets.env`
(0600); the `auth` phase does the same from `slack-bot-token`,
`slack-app-token`, `slack-channel-id`, `slack-channel-name-env` and
`slack-owner-id` in its answers file.
The scopes the doctor checks are the ones the bot calls.

The bot service comes in section 8, from the same root step that
hardens the install (docs/SETUP.md "Services"); `scripts/install/setup.sh` writes no unit.

## 7. Claude Code connectors

The lanes use the claude.ai account connectors for Gmail and Google
Calendar (and Slack on a Slack install), not servers `claude mcp add`
makes: they come with the account Claude Code logs in with. `assistant`
needs them; `email` and `review-only` only if the user has Google set
up. Ask the user to enable them in claude.ai Settings > Connectors
(claude.ai/customize/connectors) as the account ea-bot logged in with in
section 5, and to finish each one's sign-in there. Check, as ea-bot:
`sudo -n -u ea-bot env HOME=/home/ea-bot /home/ea-bot/.local/bin/claude mcp list`
lists `claude.ai Gmail` and `claude.ai Google Calendar` as connected
(docs/SETUP.md "Claude Code connectors").

## 7b. Remote writer (optional, any profile that drafts)

Ask once, after the connectors: do they run their own model, or have an
OpenAI-compatible endpoint, that should write the email bodies instead
of Claude? Most say no; then skip this screen and say nothing more. If
yes, ask for the https endpoint, the model name and the API key, then:
the `REMOTE_LLM_API_KEY=` line into `~/.ea-private/secrets.env`,
`python3 preprocessor/remote_llm.py init --endpoint <https URL> --model <name> --drafting remote`,
then `python3 preprocessor/remote_llm.py ping`. The endpoint must be
https. Do not offer the mail, digest, briefing or scheduling switches in
the same file; they are an owner's later decision (docs/SETUP.md "Remote
writer", `docs/llm-lanes.md`).

## 8. Acceptance

On Linux the hardening step, a `sudo` step the user types in the second
terminal: `sudo bash deploy/harden-permissions.sh` (docs/SETUP.md "Hardening"). It
seeds the owner-owned code tree at `/opt/ea-system` and the owner-private
files, and moves the services off the ea-bot tree so a prompt-injected
EA cannot reach the owner account. It says which bot unit it found: on
`assistant` it writes and starts the bot's system unit (docs/SETUP.md
"Services"); on a profile with no chat platform it says the bot-unit
steps are skipped, which is expected. When a service does not stay up,
it ends with a WARNING and that service's last log lines; a bot line
`the chat setting <NAME> is not set` sends you back to section 6. Check:
`test -f /opt/ea-system/.deployed-commit`, and on `assistant`
`systemctl is-active ea-chat-bot`. Mail mediation is already on:
`scripts/install/setup-ea-user.sh` ran `deploy/enable-mail-mediation.sh`.

On macOS, on every profile, two `sudo` steps the user types in the
second terminal instead: `sudo bash deploy/macos/install-services.sh` (the owner-owned
`/opt/ea-system`, the owner-private files and the LaunchDaemons: the
mediator always, the bot only with a chat platform), then
`sudo bash deploy/enable-mail-mediation.sh`, which turns mediation on
(docs/SETUP.md "launchd"). Check: the doctor's `service:` line for the
mediator passes, and
`sudo -n -u ea-bot test -r /Users/ea-bot/.ea/auth/msal_token_cache.json`
fails (ea-bot can no longer read the Outlook token).

Then install the cron lines, pointed at `/opt/ea-system`:
`./scripts/install/setup.sh --profile <name> --install-cron`. Check: `crontab -l`
shows the `run-agent.sh morning-briefing` line under `/opt/ea-system`.

Then run `python3 scripts/ea-doctor.py --profile <name>` and fix every
FAIL it prints, using the fix text next to each; each fix names the
docs/SETUP.md appendix for its check, and docs/SETUP.md "Troubleshooting and the
doctor's checks" maps every check name to one. Warnings about features
the profile does not use are expected.

Finish by running one briefing by hand,
`./run-agent.sh morning-briefing --now` (`--now` skips the 4 AM gate),
and showing the user where the report landed: the EA channel for
`assistant`, otherwise `~/.ea/state/reports/morning-briefing-latest.md`
and the phone push when one is configured.

For `email` and `review-only`, offer the phone push once (docs/SETUP.md
"Phone push"): they install the ntfy app and pick a long random topic;
`answers-push.json` (`"push-configure": true`, `push-topic`) and the
`push` phase write `~/.ea-private/push.json`;
`python3 preprocessor/push_notify.py test` proves it. The briefing above
then sends a push too.

Say three lines: the profile installed, where the EA talks to them, and
the one command that re-runs the checklist. Then go on to section 9.

## 9. First week

The EA learns the user's preferences from questions, not from guesses.
Run `python3 preprocessor/onboarding.py ask --count 3`: it prints the
first three preference questions, each with an `id`. Ask them one at a
time with AskUserQuestion, as free text ("Other"), in the user's own
words. After each answer, record it before the next question:

```bash
python3 preprocessor/onboarding.py answer --id <id> --text "<their words>"
```

Do not paraphrase the answer; the text is the rule in their words. A
scheduling answer (`protected-days`) is a standing rule for drafts and
the briefing; the scheduler skips a block only when it is an `nm` event
on the EA-Managed calendar (section 4), so say that once. If the script
is missing or prints no question, say so in one line and end the
install here.

When the three are recorded, tell the user: the morning briefing asks
one more question each day for a week, at the end of the briefing; they
answer in the thread (or in the shell on `email` and `review-only`), and
saying "stop the questions" ends the series at any time. That is the
last thing the install says.
