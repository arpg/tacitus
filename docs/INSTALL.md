# Installing Tacitus

An install has three parts. You prepare the accounts, which is mostly
waiting for your IT group. You run one command, and Claude Code walks you
through setup in about an hour (half a day for the full chat setup).
Then, for the first week, the EA asks you one question a day to learn
your preferences.

[SETUP.md](SETUP.md) is the reference behind each step. You need it only
when a step fails or you want the background; setup and the health check
point you to the right section.

## 1. Before you start

Start the IT request first. Everything else fits in one sitting.

| What | Needed for | Lead time | How |
|---|---|---|---|
| An always-on machine with `sudo` | every profile | none | Linux (a server, a NUC) or macOS. WSL2 is untested. |
| Claude Pro or Max | every profile | minutes | The EA's service account signs in with its own copy and uses your quota. |
| An Outlook app registration | every profile | days, sometimes weeks | Send [docs/it-request.md](it-request.md) to your IT group. It lists the permissions, the data flow and the security model. If you can register apps yourself, see SETUP.md "Microsoft Graph app registration". |
| A Google Cloud OAuth app | Google Calendar or Gmail | 15 minutes | SETUP.md "Google OAuth app". Google shows an "unverified app" screen at sign-in; continue through "Advanced". You are the only user. Publish the app, or Google ends the sign-in every 7 days. |
| An `EA-Managed` Google calendar | `assistant` (optional elsewhere) | 2 minutes | A secondary calendar where your scheduling rules live as recurring events. Setup tells you when to create it. |
| Docker, or a Slack workspace | `assistant` | minutes | Docker runs the Zulip chat server (recommended). See [Choosing a chat platform](#choosing-a-chat-platform). |
| A model you host | optional | varies | Any OpenAI-compatible endpoint (vLLM, Ollama). It reads mail and writes drafts in place of Claude. See [docs/remote-llm.md](remote-llm.md). |

Python 3.10 to 3.14 must be on the machine. On macOS use Homebrew's
Python; Apple's `/usr/bin/python3` is 3.9. `install.sh` checks this and
names the command that installs what is missing.

## 2. Choose a profile

| Profile | What you get | Time to install |
|---|---|---|
| `email` (start here) | Triage, the ledger, drafts in your voice, meeting times proposed in the report (no holds or events: nothing places one without the chat buttons). Briefings land in `~/.ea/state/reports/<agent>-latest.md`, with a phone push if you want one. You work with the EA in the Claude Code shell and send drafts from Outlook yourself. | About an hour |
| `review-only` | `email` without drafting: the EA reads, tracks and briefs, and writes nothing to the mailbox. A deny list in every launcher enforces it. | About an hour |
| `assistant` | Everything: chat rooms on Zulip or Slack, the Send and Create cards, the scheduling channel, plugins. | About half a day, most of it for the chat server |

Without a chat platform there are no cards, and sending needs a card's
token. So an `email` or `review-only` install cannot send mail or create
an event at all. To move up later, run `./scripts/install/setup.sh --profile assistant`,
then `claude "/setup"` in the checkout. Setup skips what is already
done.

## 3. Run the installer

On the machine the EA will run on:

```bash
git clone https://github.com/arpg/tacitus.git ea-system && cd ea-system && ./install.sh
```

Once the repository is public, this also works from anywhere:

```bash
curl -fsSL https://raw.githubusercontent.com/arpg/tacitus/main/install.sh | bash
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
| `EA_REPO=<url>` | Clone from another URL, for example `git@github.com:arpg/tacitus.git` while the repository is private |
| `EA_PYTHON=<path>` | Use this Python when the first one on PATH is too old or too new |

### Keep a second terminal open

A few steps need your password, your browser or a full-screen login:
the `sudo` steps (two on Linux, three on macOS), the Google sign-in, and
the service account's Claude login. Claude Code cannot type those for
you. Setup asks you to open a second terminal in the checkout, gives you
the exact line to run there, and checks the result before it goes on.

### What setup asks

Setup asks one question at a time and runs every command itself.

1. **Profile.** `email`, `review-only` or `assistant`.
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
6. **Chat** (`assistant` only). The Zulip server or the Slack app.
7. **Extras** (optional). Claude Code connectors, and the model you host.
8. **Acceptance.** Locks down permissions, installs the scheduled jobs,
   runs the health check and one briefing so you can see where it lands.
9. **First week.** Three preference questions, in your own words.

### Choosing a chat platform

The `assistant` profile needs a chat platform. The chat carries the most
sensitive text you have: quoted mail, draft replies, calendar detail.
The platform decides who holds that text.

| | Zulip on your machine (recommended) | Slack |
|---|---|---|
| Where the conversation lives | A Docker volume on your machine, inside your backups | Slack's servers, under Slack's terms and retention |
| What leaves your machine | Push notices with the content removed | Every message, file and card, with drafts and quoted mail |
| History | Unlimited and yours | 90 days visible on the free tier |
| Approvals | One reaction chip per action, or a typed code | Buttons, and an Edit dialog |
| Phone | The Zulip app over your tailnet | The Slack app |
| Setup | SETUP.md "Zulip server" | SETUP.md "Slack" |

The EA behaves the same on both. Switching is one config key and a
restart ([docs/zulip-switch.md](zulip-switch.md)).

## 4. Check the install

```bash
python3 scripts/ea-doctor.py --profile <name>
```

Then the root steps, all in one command (Linux):

```bash
sudo bash /opt/ea-system/deploy/apply-host.sh
```

The doctor prints PASS, WARN or FAIL for each part. Each FAIL carries
its fix and the SETUP.md section that explains it. Warnings about
features your profile does not use are expected. Run it again at any
time.

To see a briefing now, without waiting for the morning:

```bash
./run-agent.sh morning-briefing --now
```

On `assistant` it posts in the `ea` channel. Otherwise it is in
`~/.ea/state/reports/morning-briefing-latest.md`, and on your phone if
you set up the push.

## 5. The first week

The morning briefing ends with one preference question a day for a
week. Answer in the thread (or in the shell on `email`). The EA stores
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

**Docker.** The EA and the Zulip server run as containers on one host.
[docs/docker.md](docker.md) covers the first run, the volumes and
the differences from a host install.

**Without Claude Code.** The `scripts/install/user_init.py` wizard writes your
preferences and config files interactively. It does not create the
service account, the services or the scheduled jobs; finish those with
SETUP.md.

```bash
python3 scripts/install/user_init.py                       # every phase
python3 scripts/install/user_init.py --phase scheduling    # one phase again
python3 scripts/install/user_init.py --style-only          # measure your writing style again
python3 scripts/install/user_init.py --update              # every phase, showing current values
```

## Upgrade

From your checkout, on `main`:

```bash
git pull
sudo bash /opt/ea-system/deploy/apply-host.sh
python3 scripts/ea-doctor.py
```

`apply-host.sh` is the one root step, after setup and after every update.
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
`./scripts/install/setup.sh --profile <name> --install-cron`. It adds new templates and
keeps the files you have. In Docker: `git pull`, `docker compose build`,
then `docker compose up -d`.

## Uninstall

Stop the services and jobs, remove what the installers created, and keep
or delete your data. Run these as your own account, with `sudo` where
shown.

On Linux:

```bash
crontab -e                                   # delete the "EA System" and "EA Plugin" lines
sudo systemctl disable --now ea-chat-bot ea-mail-mediator
sudo rm -rf /etc/systemd/system/ea-chat-bot.service* /etc/systemd/system/ea-mail-mediator.service*
sudo systemctl daemon-reload
sudo rm -f /etc/sudoers.d/ea-bot /etc/sudoers.d/ea-chat-bot
sudo rm -rf /opt/ea-system
rm ~/.ea && sudo mv /home/ea-bot/.ea ~/.ea && sudo chown -R "$(id -un)": ~/.ea   # keep your data
sudo userdel -r ea-bot && sudo groupdel ea   # removes /home/ea-bot (its code copy and Claude Code)
chmod o+x ~                                  # only if your home was open before
```

On macOS:

```bash
crontab -e                                   # delete the "EA System" and "EA Plugin" lines
sudo bash deploy/enable-mail-mediation.sh --rollback
sudo bash deploy/macos/install-services.sh --remove   # the LaunchDaemons, the launcher, its sudoers file
sudo rm -f /etc/sudoers.d/ea-bot
sudo rm -rf /opt/ea-system
rm ~/.ea && sudo mv /Users/ea-bot/.ea ~/.ea && sudo chown -R "$(id -un)": ~/.ea   # keep your data
sudo dscl . -delete /Users/ea-bot && sudo dscl . -delete /Groups/ea && sudo rm -rf /Users/ea-bot
```

These stay until you delete them: `~/.ea` (the ledger, the config, and
the tokens in `auth/`), `~/.ea-private` (your secrets, the push and chat
settings, the owner logs), the checkout, your own Claude Code
(`~/.local/bin/claude`), and the Python packages with the
`~/.local/share/ea-system` shim. Also revoke the EA's access where you
granted it: the Microsoft app's consent (or ask IT to remove the
registration), the Google OAuth client, the claude.ai connectors, and on
`assistant` the Slack app or the Zulip bot. A Docker install is
`docker compose down -v` (the `-v` deletes the volumes and every token
in them).

## If something fails

1. Run the doctor and follow the fix next to each FAIL.
2. Look the check up in SETUP.md "Troubleshooting and the doctor's
   checks", which also lists the warnings a new install expects.
3. For a running install that stopped working, see
   [docs/RUNBOOK.md](RUNBOOK.md).
4. Still stuck: open an issue with the **Install or first-week
   feedback** form. It asks for the version, the OS, the profile, the
   step and the doctor's output. Replace real names, addresses and mail
   text with made-up values.
