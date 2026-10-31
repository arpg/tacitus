# Contributing

Thank you for your interest in this project. It is research software,
maintained on a best-effort basis.

## Reporting a problem

- An install that never got going: the **Install or first-week
  feedback** issue form.
- Something that stopped working: the **Bug report** form, with the
  output of `python3 scripts/ea-doctor.py --share`, which masks your
  addresses, account names, hosts and ids.
- A question: [Discussions > Q&A](https://github.com/arpg/tacitus/discussions/categories/q-a).
- An idea: [Discussions > Ideas](https://github.com/arpg/tacitus/discussions/categories/ideas);
  it becomes an issue when it has a design and someone to build it.
- A vulnerability: a private security advisory ([SECURITY.md](SECURITY.md)).

In every report, replace real names, addresses and mail text with
made-up values. An issue labelled `needs-info` closes after three weeks
without an answer; reopen it when you have the details.

## What gets merged

The maintainer's time goes, in this order, to the approval gates, the
mediator and anything that moves data off the machine; to install
failures on Linux and macOS; then to the rest. A pull request with
tests, a clear mechanism and no new dependency is the fastest route for
anything else.

A capability for one person, one institution or one service (a form to
file, a portal to read, a list to digest, an institution's letterhead)
is a plugin, not a core change: [docs/plugins.md](docs/plugins.md). A
core change that adds a dependency, a new lane that spends model tokens,
or a change to the operating file `CLAUDE.md` needs an Ideas discussion
first; the operating file is read on every turn of every install.

## Before you start

- The [code of conduct](CODE_OF_CONDUCT.md) (Contributor Covenant 2.1)
  applies in issues, Discussions and pull requests.
- Open an issue first for a change larger than a small fix.
- Read `docs/SECURITY.md`. The approval gates (the Send and Create buttons),
  the mail mediator and the separate OS user are the core of the design. A
  change that weakens them will not be merged.
- Read `docs/plugins.md`. A capability for one person or one institution
  belongs in a plugin, not in the core.

## Making a change

1. Fork the repository (or clone it, if you have write access), make a
   branch, and open a pull request from it.
2. Turn the hooks on in your clone: `git config core.hooksPath .githooks`
   (`scripts/install/setup.sh` does it on an install). The pre-commit hook runs a secrets
   lint and a boundary lint; fix what they report and do not bypass
   them. The post-commit hook deploys only from the main checkout on
   `main`, and runs the test wall there.
3. Match the code around your change. Comments state what the code does,
   in present tense.
4. Add or update tests. Run the wall, every test file the way CI runs
   it: `python3 tests/run_wall.py` (about ten minutes). One file:
   `python3 -m pytest -q tests/test_<name>.py`.
5. Never commit real names, email addresses or mail text, in code,
   tests, commit messages or pull request descriptions. Use made-up
   values, and describe a bug by its mechanism and its date. Claude Code
   sessions in a checkout also read `.claude/CLAUDE.md`, the developer
   guide; the root `CLAUDE.md` is the EA's operating file.

## Where things are

There are three entry points: `run-agent.sh` (the scheduled agents),
`ea-chat-bot.py` (the chat listener; the name predates Zulip) and
`run-plugin.sh` (plugins). Each one runs Claude Code as a subprocess
under the `ea-bot` user.

```
ea-system/                     # this repository
├── ea-chat-bot.py             # chat listener (Zulip or Slack): rooms, cards, the review flow
├── run-agent.sh               # scheduled agents: briefings, audits, intake, list digests
├── run-plugin.sh              # scheduled plugins, run as ea-bot
├── CLAUDE.md                  # the operating core every lane reads; topic guides in docs/ea/
├── prompts/                   # the scheduling channel's operating files
├── preprocessor/              # Python that spends no model tokens
│   ├── chat/                  # chat front end: the interface, the Slack and Zulip adapters, cards
│   ├── preprocess.py          # mail and calendar delta engine, request detection
│   ├── closure_scrubber.py    # closes ledger items from evidence before each briefing
│   ├── ledger.py              # the obligations ledger (SQLite)
│   ├── project_store.py       # projects, people, journal, context packs
│   ├── preferences.py         # standing rules in the user's words, by scope
│   ├── scheduling_engine.py   # OR-tools solver, reschedule briefs, polls
│   ├── graph_client.py        # Microsoft Graph (Outlook)
│   ├── graph_actions.py       # the CLI Claude uses to act on mail and calendar
│   ├── gmail_client.py        # Gmail drafts, threading, attachments
│   ├── mail_mediation.py      # the SAFE and GATED verb table; the ea-bot side of the mediator
│   ├── mail_mediator.py       # the owner-side service that holds the tokens
│   ├── resident_bridge.py     # the long-lived resident session
│   ├── slack_sessions.py      # thread-to-session registry, conversation index
│   ├── ea_channels.py         # project and session room registry
│   └── ...
├── ea_plugins/                # helpers a plugin imports: scoped ledger, mediated mail
├── skills/                    # instruction sets for the scheduled agents
├── docs/                      # INSTALL.md, SETUP.md, ARCHITECTURE.md, SECURITY.md, the runbook
├── docs/ea/                   # topic guides appended to CLAUDE.md when a message needs them
├── .claude/agents/            # the email-author subagent
├── .claude/skills/setup/      # the /setup install skill
├── config/                    # templates, copied to ~/.ea/config/ at install
├── deploy/                    # root steps: mail mediation, hardening, deck builder; zulip/ runs the chat server
├── scripts/                   # ea-bot shell, doctor, deny lists, Zulip bootstrap and switch
│   └── install/               # setup.sh, the service-account installers, user_init.py
└── tests/                     # the test wall, prompt evals, smoke test, lints

~/.ea/                         # per-user state, shared with ea-bot (not in the repository)
├── .env                       # settings only; ea-bot can write it, so never a secret
├── auth/                      # OAuth tokens, owned by you, unreadable to ea-bot
├── config/                    # your preferences
├── state/                     # ledger and project store (SQLite), briefings, staged actions
├── docs/<project>/            # documents the EA writes, by project
├── research/<topic>/          # folders both you and the EA write
├── plugins/<name>/            # agent-zone plugins
└── logs/

~/.ea-private/                 # owner-only (0700): secrets, chat settings, card tokens, owner logs
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the data flow, prompt
construction and approval gates; its File Index lists every module.

## Tests

```bash
python3 tests/run_wall.py                 # every test file, the way CI runs them
python3 -m pytest -q tests/test_<name>.py # one file
python3 tests/evals/run_evals.py --verbose  # prompt evals: real prompt assembly, judged against fixtures
./tests/smoke_test.sh                     # health of a full install
./tests/secrets_lint.sh                   # no credentials in the source
python3 tests/validate_config.py          # config files are complete
```

On `main` in the deploying checkout, the post-commit hook copies each
commit to the bot's tree, runs the wall and the evals, and restarts the
bot only when running code changed, after any thread in progress ends.
CI lints every pull request once it is ready for review; a draft runs
nothing. A push to `main` that passes the full tiers is a release: CI
signs the next `vX.Y.Z` tag on it, and installs pick it up within the
hour ([docs/updates.md](docs/updates.md)). The patch number goes up by
default; `#minor` or `#major` as a word of its own in the pull request's
title or body raises that part.
A step each install's owner must take by hand goes in a line `Owner-steps: <the step>`
in the pull request body or a commit message; the release notes list it.
The full tiers (the wall on Linux and macOS, the Docker wall, the install
smoke) run only when a maintainer adds the `full-ci` label, or with
Actions > CI > Run workflow on a branch. Run the wall locally
before you ask for it.

## Running a session as the bot

To reproduce the bot's behavior, run a session as `ea-bot` with sending
blocked:

```bash
bash scripts/ea-bot-shell.sh                       # interactive
bash scripts/ea-bot-shell.sh "<prompt>"            # one prompt
bash scripts/ea-bot-shell.sh --resume <uuid>       # continue a session
bash scripts/ea-bot-shell.sh --thread <thread_ts>  # continue a chat thread's session
EA_SHELL_MODEL=<model id> bash scripts/ea-bot-shell.sh   # pin a model
```

The shell starts Claude as `ea-bot` with the bot's tools, credentials,
working directory and deny list. Every button-required command
(`send-draft`, `create-event`, `update-event`, `delete-event`,
`schedule-send`, `send-gmail-draft`, `request-confirm`) is refused;
drafting, reading mail, the ledger and the scheduling checks all work.
A new session prints its id: `adopt session <uuid>` in a chat thread
continues it there, and `open a channel for session <uuid>` gives it a
room.

## Contributor license

Tacitus is licensed under the GNU Affero General Public License, version
3 only (`LICENSE`). Its authors may also offer it under other terms, for
example a commercial license. So that the project can do that, every
contribution comes in under these terms:

1. You keep the copyright in your contribution.
2. You grant the maintainer named in `AUTHORS` a perpetual, worldwide,
   non-exclusive, royalty-free and irrevocable license to use, copy,
   change, distribute and sublicense your contribution, and to license it
   under any terms, including AGPL-3.0 and commercial licenses. The
   maintainer may transfer this license to an entity that takes over the
   project.
3. You grant the same license under any patent claim that you can
   license and that your contribution would otherwise infringe.
4. You wrote the contribution or have the right to contribute it, and no
   employer, grant or other license prevents you from giving it under
   these terms.
5. Your contribution comes "as is", without warranty.

To accept these terms, write this line in your first pull request:

    I agree to the contributor license in CONTRIBUTING.md.

Each commit also carries a sign-off line that certifies you wrote the
change:

    Signed-off-by: Your Name <you@example.com>

`git commit -s` adds the line for you.
