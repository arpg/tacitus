# Installing Tacitus

An install has three parts. You prepare the accounts, which is mostly
waiting for your IT group. You run one command, and Claude Code walks you
through setup, on a page in your browser or in the terminal. Then, for
the first week, the EA asks you one question a day to learn your
preferences.

Setup runs every command it can. A fresh `assistant` install with the
web chat, on Linux, needs twelve steps from you once IT has registered
the app: the commands that need your password or a full-screen login,
the sign-ins in a browser, and your phone. [Your steps](#your-steps)
lists them. A `reviewer` install without Google needs six.

[SETUP.md](SETUP.md) is the reference behind each step. You need it only
when a step fails or you want the background; setup and the health check
point you to the right section.

## 1. Before you start

Start the IT request first. Everything else fits in one sitting.

| What | Needed for | Lead time | How |
|---|---|---|---|
| An always-on machine with `sudo` | every mode | none | Linux (a server, a NUC) or macOS. WSL2 is untested. |
| Claude Pro or Max | every mode | minutes | The EA's service account signs in with its own copy and uses your quota. |
| An Outlook app registration | every mode | days, sometimes weeks | Send [docs/it-request.md](it-request.md) to your IT group. It lists the permissions, the data flow and the security model. If you can register apps yourself, see SETUP.md "Microsoft Graph app registration". |
| A Google Cloud OAuth app | Google Calendar or Gmail | 15 minutes | SETUP.md "Google OAuth app". Google shows an "unverified app" screen at sign-in; continue through "Advanced". You are the only user. Publish the app, or Google ends the sign-in every 7 days. |
| An `EA-Managed` Google calendar | `assistant` and `collaborator` (optional on `reviewer`) | 2 minutes | A secondary calendar where your scheduling rules live as recurring events. Setup tells you when to create it. |
| Tailscale on the machine and on your phone | `assistant` and `collaborator` with the web chat | minutes | The bot serves the web chat on your tailnet. See [Choosing a chat platform](#choosing-a-chat-platform). In Docker, a TLS proxy and a host name take its place ([docs/docker.md](docker.md#web-chat)). |
| A Slack workspace | `assistant` and `collaborator` on Slack instead | minutes | One private channel and one bot app (SETUP.md "Slack"). |
| A model you host | optional | varies | Any OpenAI-compatible endpoint (vLLM, Ollama). It reads mail and writes drafts in place of Claude. See [docs/remote-llm.md](remote-llm.md). |

Python 3.10 to 3.14 must be on the machine. On macOS use Homebrew's
Python; Apple's `/usr/bin/python3` is 3.9. `install.sh` checks this and
names the command that installs what is missing.

## 2. Choose a mode

Setup asks this first. `assistant` is the default.

| Mode | What you get | Time to install |
|---|---|---|
| `reviewer` | No chat. Triage, the ledger, drafts in your voice left in your mailbox, meeting times proposed in the report (no holds or events: nothing places one without the chat buttons). Reports land in `~/.ea/state/reports/<agent>-latest.md` on the schedule you pick: weekdays, every day, or only when you run `./run-agent.sh morning-briefing --now`. A phone push if you want one. You work with the EA in the Claude Code shell and send drafts from Outlook yourself. | About an hour |
| `assistant` | One chat on your phone: the web chat (or Slack) with the EA channel, your DM with the Send and Create cards, scheduling and digests in the same app, plugins. No project rooms. | Longer: the twelve steps below. |
| `collaborator` | The assistant, plus a room for each big project. | The same as `assistant`. |

Without a chat there are no cards, and sending needs a card's token. So
a `reviewer` install cannot send mail or create an event at all. For a
reviewer that writes no drafts either, set `"drafting": false` in
`~/.ea/config/ea-config.json`; a deny list in every launcher enforces it.

To add the chat later, run
`./scripts/install/setup.sh --profile assistant --install-cron` (it
records the new mode), then `claude "/setup"` in the checkout. Setup
skips what is already done. Between `assistant` and `collaborator`
nothing else changes:
`python3 preprocessor/ea_config.py set-profile collaborator` turns the
project rooms on, and `set-profile assistant` turns them off.

## 3. Run the installer

On the machine the EA will run on:

```bash
git clone https://github.com/arpg/tacitus.git ea-system && cd ea-system && ./install.sh   # clone, install, open setup
```

Or from anywhere, with no checkout yet:

```bash
curl -fsSL https://raw.githubusercontent.com/arpg/tacitus/main/install.sh | bash   # the same installer, fetched
```

`install.sh` checks git, curl, Python and sudo, and names the command for
anything missing. It installs Claude Code if it is not on your PATH,
clones the repository to `~/ea-system` when you are not already in a
checkout, installs the Python packages, and opens Claude Code on the
`/setup` skill. Running it again says what is already done and moves on.

| Option | Effect |
|---|---|
| `./install.sh --check` | Print the prerequisite table only |
| `./install.sh --no-launch` | Install, then print the two commands to start setup by hand (`cd ~/ea-system`, then `claude "/setup"`) |
| `EA_SRC=<dir>` | Clone somewhere other than `~/ea-system` |
| `EA_REPO=<url>` | Clone from another URL, for example your fork |
| `EA_PYTHON=<path>` | Use this Python when the first one on PATH is too old or too new |

### Keep a second terminal open

Some steps need your password, a full-screen login or a pasted address:
the `sudo` steps, the Google sign-in and the service account's Claude
login (steps 4, 6, 7, 8, 10 and 11 below). Claude Code cannot type those
for you. Setup asks you to open a second terminal in the checkout, gives
you the exact line to run there, and checks the result before it goes
on.

### What setup asks

Setup first offers a page in your browser: the same questions, one
screen per topic, on your phone or laptop ([docs/setup-web.md](setup-web.md)).
The page writes the same files as the terminal and signs in to nothing;
setup goes on from where the page stops. Or answer in the terminal, one
question at a time.

1. **Mode.** `reviewer`, `assistant` or `collaborator`, and the "Your
   responsibility" notice from the README. A reviewer also picks when its
   reports come and whether it leaves drafts.
2. **You and your institution.** Name, work address, role; your
   institution's mail domains and video platform. A first email to
   anyone outside those domains always waits for your approval.
3. **Outlook.** The ids from your IT group, then a sign-in in the
   browser. If IT has not answered yet, setup fills in the request for
   you and stops. Run `claude "/setup"` in the checkout when IT answers;
   setup resumes where it stopped.
4. **Google** (optional). The accounts to read, a sign-in, the
   `EA-Managed` calendar, and your working hours.
5. **The service account.** Creates the `ea-bot` user that Claude runs
   as, and turns on the mail mediator.
6. **Chat** (`assistant` and `collaborator`). Setup asks which: the web chat
   (recommended, self-hosted) or Slack. For the web chat it asks this
   machine's tailnet name and your Tailscale login, writes the settings
   and gives you the `tailscale serve` line (step 8 below).
7. **Extras** (optional). Claude Code connectors, and the model you host.
8. **Acceptance.** Locks down permissions, installs the scheduled jobs,
   runs the health check and one briefing so you can see where it lands.
9. **First week.** Three preference questions, in your own words.

### Your steps

Setup cannot type your password, sign in for you in a browser, or hold
your phone. These are the steps it hands to you, in install order, for
a fresh `assistant` install with the web chat on Linux. Each line says
where you do it and what it does.

1. **Start the installer.** In a terminal on the machine:
   `./install.sh` (section 3). It installs Claude Code and the Python
   packages, then opens setup.
2. **Sign in to Outlook.** In Claude Code, with a `!` in front:
   `! python3 preprocessor/graph_client.py --align --write`. It prints a
   link for your browser and keeps the EA's permissions to what the app
   grants.
3. **Make the Google OAuth app.** In the Google Cloud console: a project,
   the Calendar API, the consent screen published, and a Desktop client
   whose JSON you save as `~/.ea/auth/google_client_secret.json`
   (SETUP.md "Google OAuth app").
4. **Sign in to Google.** In the second terminal:
   `python3 preprocessor/google_calendar_client.py --setup` signs in each
   Google account the EA reads.
5. **Make the EA-Managed calendar.** In Google Calendar: a secondary
   calendar named `EA-Managed` with one recurring `wh Working Hours`
   event. The solver proposes times only inside it.
6. **Create the service account.** In the second terminal:
   `sudo ./scripts/install/setup-ea-user.sh` creates the `ea-bot` user
   that the model runs as, and turns mail mediation on.
7. **Log ea-bot's Claude Code in.** In the second terminal:
   `sudo -u ea-bot env HOME=/home/ea-bot claude`, then `/login`, then
   Ctrl+C. The EA works on this login.
8. **Publish the web chat on your tailnet.** In the second terminal:
   `sudo tailscale serve --bg --https=8444 --set-path /chat http://127.0.0.1:8470/chat`
   gives the chat an HTTPS address of its own on your tailnet (SETUP.md
   "Web chat").
9. **Turn on the connectors.** On claude.ai, as the account ea-bot
   logged in with: Gmail and Google Calendar under Settings > Connectors.
10. **Harden and start the bot.** In the second terminal:
    `sudo bash deploy/harden-permissions.sh` moves the services to
    `/opt/ea-system`, has you type back the web chat's address and your
    login as it copies the chat settings into your private directory,
    then writes and starts the bot's service.
11. **Run the remaining root steps.** In the second terminal:
    `sudo bash /opt/ea-system/deploy/apply-host.sh` runs each root script
    that the boundary audit still finds missing.
12. **Install the app on your phone.** Open the chat's address, add it to
    the Home Screen, and turn on Notifications in the channel drawer.

The count leaves out the IT request, which comes before the install,
and the questions you answer, since setup writes the files. A `reviewer`
install without Google skips steps 3, 4, 5, 8, 9 and 12. A
`collaborator` install has the same twelve steps as `assistant`. On macOS, step
6 is `sudo ./scripts/install/setup-ea-user-macos.sh`, step 7 is
`claude setup-token` (SETUP.md "macOS notes"), and steps 10 and 11 are
`sudo bash deploy/macos/install-services.sh` and
`sudo bash deploy/enable-mail-mediation.sh` (SETUP.md "launchd").

### Use Tacitus on your other devices

Once the chat runs on one device, add the others from it.

- **With Tailscale** on the other device: sign it in to your tailnet and
  open the chat's address (`https://<host>.<tailnet>.ts.net:8444/chat`).
  Your Tailscale login signs you in. On a phone, add it to the Home
  Screen.
- **Without Tailscale** (a work laptop, a computer you cannot install
  on): this needs the relay (SETUP.md "Web chat", step 6). On a device
  that is already signed in, open the menu, tap **Settings**, then
  **Devices** and **A computer**. It shows a code and an address. On the computer, open
  that address (`tacitus-ai.github.io`) and type the code. The computer
  shows a six-digit code: tap **Approve** on the first device only when
  it shows the same six digits. Never send the code through chat or mail.

A computer set up the second way runs the app from the hosted site, so
you trust that site when you set it up and at each update of the app,
as you trust the relay's copy ([docs/webchat-relay.md](webchat-relay.md),
"The hosted app"). Send and Create buttons stay off on it unless you
allow them when you approve.

### Choosing a chat platform

The `assistant` and `collaborator` modes need a chat platform. The chat carries the most
sensitive text you have: quoted mail, draft replies, calendar detail.
The platform decides who holds that text.

| Platform | Where the conversation lives | What leaves your machine | History | Approvals | Phone | Setup |
|---|---|---|---|---|---|---|
| The web chat (recommended) | A file in your owner-private directory, inside your backups | Notifications, encrypted to your browser; off the tailnet, sealed relay frames | Unlimited and yours | Buttons on each card | The installed web app over your tailnet, or through the relay | SETUP.md "Web chat": a settings block and one `tailscale serve` line |
| Slack | Slack's servers, under Slack's terms and retention | Every message, file and card, with drafts and quoted mail | 90 days visible on the free tier | Buttons, and an Edit dialog | The Slack app | SETUP.md "Slack" |

The EA behaves the same on both. The web chat is self-hosted: the bot
serves it, and no chat server or third party holds the conversation.
Use Slack if you already live there and accept its terms. A Slack
install can also run the web chat beside it (shadow mode, SETUP.md
"Beside Slack (shadow mode)"): each post appears in both, and you answer
in either.

New chat features land in the web chat. Slack keeps working as it is
and gets fixes, not new features. On its own, the web chat carries every
post: the briefings and the plugins' posts land in the `ea` channel, the
mail digests in a channel of their own, scheduling requests from mail
open as conversations in its Scheduling channel, and the doctor checks
its settings.

## 4. Check the install

```bash
python3 scripts/ea-doctor.py --profile <name>   # check each part the mode needs
```

The doctor prints PASS, WARN or FAIL for each part. Each FAIL carries
its fix and the SETUP.md section that explains it. Warnings about
features your mode does not use are expected. Run it again at any
time. On Linux, the root steps are step 11 above, and one command runs
whichever are still missing:

```bash
sudo bash /opt/ea-system/deploy/apply-host.sh   # run each root step the boundary audit finds missing
```

To see a briefing now, without waiting for the morning:

```bash
./run-agent.sh morning-briefing --now   # one briefing now; --now skips the 4 AM gate
```

On `assistant` and `collaborator` it posts in the `ea` channel. On
`reviewer` it is in `~/.ea/state/reports/morning-briefing-latest.md`, and
on your phone if you set up the push. The same command gives a reviewer
a report whenever it wants one.

## 5. The first week

The morning briefing ends with one preference question a day for a
week. Answer in the thread (or in the shell on `reviewer`). The EA stores
each answer in your words as a standing rule. Say "stop the questions"
to end the series.

What you can change later, by asking the EA or by editing the file:

| Setting | What it controls | Where it lives |
|---|---|---|
| Inbox mode | Whether every message is an action item, and whether archiving closes the item | `~/.ea/config/ea-config.json` |
| Stale-thread threshold | Days without a reply before a thread counts as dropped | `~/.ea/config/ea-config.json` |
| Working hours, lunch, no-meeting days, priorities | When and how the solver proposes meetings | Recurring events on the `EA-Managed` calendar |
| Per-person scheduling, VIPs | Durations, buffers and priority by person | `~/.ea/config/scheduling-preferences.json` |
| Autonomy | What the EA does without asking. Sending, creating events and deleting always need your click. | `~/.ea/config/autonomy-tiers.json` |
| Standing rules | Your preferences in your words, by scope | `~/.ea/config/preferences.json`, written when you state a rule in chat |
| Mailing lists | Lists summarized as a digest instead of triaged one by one | `~/.ea/config/list-digests.json` |
| Writing style | Greeting, closing, tone and length per person | Measured from your sent mail each week |

## Other ways to install

**Docker.** The EA runs as containers on one host, with no account,
unit or crontab on the host. On `assistant` and `collaborator` the bot container serves the
web chat behind a TLS proxy you run (a Caddy example ships beside
`compose.yaml`), so no chat server is needed.
[docs/docker.md](docker.md) covers the first run, the volumes, the web
chat and the differences from a host install.

**Without Claude Code.** The `scripts/install/user_init.py` wizard writes your
preferences and config files interactively. It does not create the
service account, the services or the scheduled jobs; finish those with
SETUP.md. The setup page, `python3 scripts/install/setup_web.py`, asks
the same questions in a browser ([docs/setup-web.md](setup-web.md)).

```bash
python3 scripts/install/user_init.py                       # every phase
python3 scripts/install/user_init.py --phase scheduling    # one phase again
python3 scripts/install/user_init.py --style-only          # measure your writing style again
python3 scripts/install/user_init.py --update              # every phase, showing current values
```

## Upgrade

**Updates install themselves.** On an install with an update channel (a
new install's template says `auto`), an hourly job installs each new
signed release once nothing is running, and switches back on its own
when the release does not come up healthy. The morning briefing says
`Updated to vX.Y.Z` once, the doctor's `updates` row and the `version`
chat command say what runs, and `python3 /opt/ea-system/scripts/update.py
status` says the rest. [docs/updates.md](updates.md) is the design, and
says how to turn updates on for an install made before them.

An install with no channel (no `update` key in `ea-config.json`) updates
from its checkout. From your checkout, on `main`:

```bash
git pull                                       # fetch the new code; the post-merge hook deploys it
sudo bash /opt/ea-system/deploy/apply-host.sh  # run any root step the new code needs
python3 scripts/ea-doctor.py                   # check the result
```

On macOS the root step after an update is
`sudo bash deploy/macos/install-services.sh` from your checkout, which
rewrites the LaunchDaemons and the launcher; `apply-host.sh` there runs
only mail mediation and prints the Linux-only steps as not available.

`apply-host.sh` is the one root step on Linux, after setup and after every update.
It runs the boundary audit, runs only the root scripts whose checks fail
(hardening, mail mediation, the auth pin, the network policy, ssh, the
process view), says in plain words what each one does and why, and checks
again. `--plan` shows what it would run and changes nothing.

The pull runs the post-merge hook (setup turned it on with
`git config core.hooksPath .githooks`). The hook copies the new code to
the service account and to `/opt/ea-system`, restarts the bot only when
running code changed, and runs the tests and the prompt evals
(`EA_POSTCOMMIT_TESTS=0`, or `"eval_postcommit_enabled": false` in the
owner-private `~/.ea-private/evals.json`, skips them). The
doctor then checks the result.

**From the pre-release line (v1.2.0 and earlier) on the Outlook web
token path** (`GRAPH_AUTH_METHOD=playwright`, a pasted token, or a cron
line that runs `token_refresh.py`): v0.1.0 moved that path to the owner
plugin [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth).
Install it and replace the old cron line as its README says ("Moving from
the core's copy"), or Outlook access stops when the current token
expires. The doctor fails `auth: outlook web token path` until then.

After a change to the config templates or the scheduled jobs, also run
`./scripts/install/setup.sh --profile <name> --install-cron`. It adds new
templates, keeps the files you have, and installs the new cron lines. In
Docker: `git pull` (the new code), `docker compose build` (the new
image), then `docker compose up -d` (the containers on it).

## Uninstall

Stop the services and jobs, remove what the installers created, and keep
or delete your data. Run these as your own account, with `sudo` where
shown.

On Linux:

```bash
crontab -e                                   # delete the "EA System" and "EA Plugin" lines
sudo systemctl disable --now ea-chat-bot ea-mail-mediator   # stop both services for good
sudo rm -rf /etc/systemd/system/ea-chat-bot.service* /etc/systemd/system/ea-mail-mediator.service*   # their units and drop-ins
sudo systemctl daemon-reload                 # make systemd forget the units
sudo tailscale serve --https=8444 --set-path /chat off   # stop publishing the web chat
sudo rm -f /etc/sudoers.d/ea-bot /etc/sudoers.d/ea-chat-bot   # the two sudoers rules
sudo rm -rf /opt/ea-system                   # the owner-owned code copy
rm ~/.ea && sudo mv /home/ea-bot/.ea ~/.ea && sudo chown -R "$(id -un)": ~/.ea   # keep your data
sudo userdel -r ea-bot && sudo groupdel ea   # removes /home/ea-bot (its code copy and Claude Code)
chmod o+x ~                                  # only if your home was open before
```

On macOS:

```bash
crontab -e                                   # delete the "EA System" and "EA Plugin" lines
sudo bash deploy/enable-mail-mediation.sh --rollback   # turn mail mediation off
sudo bash deploy/macos/install-services.sh --remove   # the LaunchDaemons, the launcher, its sudoers file
sudo rm -f /etc/sudoers.d/ea-bot             # the sudoers rule for ea-bot
sudo rm -rf /opt/ea-system                   # the owner-owned code copy
rm ~/.ea && sudo mv /Users/ea-bot/.ea ~/.ea && sudo chown -R "$(id -un)": ~/.ea   # keep your data
sudo dscl . -delete /Users/ea-bot && sudo dscl . -delete /Groups/ea && sudo rm -rf /Users/ea-bot   # the user, the group, its home
```

These stay until you delete them: `~/.ea` (the ledger, the config, and
the tokens in `auth/`), `~/.ea-private` (your secrets, the push and chat
settings, the web chat's history and its nightly snapshots in
`backups/webchat/`, the owner logs), the checkout, your own Claude Code
(`~/.local/bin/claude`), and the Python packages with the
`~/.local/share/ea-system` shim. Also revoke the EA's access where you
granted it: the Microsoft app's consent (or ask IT to remove the
registration), the Google OAuth client, the claude.ai connectors, and
the Slack app if you used Slack. A Docker install is
`docker compose down -v` (the `-v` deletes the volumes and every token
in them).

Before you delete `~/.ea-private`, keep a copy of the web chat if you
want its history: `scripts/webchat-backup.sh --label final` writes one
snapshot to `~/.ea-private/backups/webchat/<date>-final/` (the store, its
uploads, the relay key, the pairing token); copy that folder somewhere
private.

## If something fails

1. Run the doctor and follow the fix next to each FAIL.
2. Look the check up in SETUP.md "Troubleshooting and the doctor's
   checks", which also lists the warnings a new install expects.
3. For a running install that stopped working, see
   [docs/RUNBOOK.md](RUNBOOK.md).
4. Still stuck: open an issue with the **Install or first-week
   feedback** form. It asks for the version, the OS, the mode, the
   step and the doctor's output. Replace real names, addresses and mail
   text with made-up values.
