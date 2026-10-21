# Running the EA in Docker

The EA runs as a small set of containers from one image. The image keeps
the security split of a host install: two users, the tokens with the
owner, Claude with `ea-bot`. You need no `ea-bot` account, no systemd unit
and no crontab on the host.

This is stage 1: one image with both users and a sudo rule inside it.
Stage 2 is at the end of this page and is not built.

## What runs

| Service | User | What it does | Starts with |
|---|---|---|---|
| `init` | root, then ea-bot | Checks the volumes, prepares ea-bot's space as ea-bot (config templates, copied-in files, the profile, mail mediation), fixes the owner's private volume, renders the install overlay, then exits. | every `up` |
| `mediator` | owner | The mail mediator. It holds the Graph and Gmail tokens and runs the agent's safe mail operations. | `up` |
| `scheduler` | owner | supercronic with the cron jobs of your install profile. | `up` |
| `bot` | owner | The chat bot. | `--profile assistant` |
| `zulip` and its four helpers | (their own) | A self-hosted Zulip, HTTP on `127.0.0.1:8090`. | `--profile zulip` |
| `setup` | root, then ea-bot and the owner in turn | The first configuration, once: the config questions as `ea-bot`, `~/.ea/.env` and the owner's secrets, the Claude login as `ea-bot`, the mail sign-in as the owner, the overlay, the doctor. | `run` only |
| `agent` | ea-bot | A shell or a command as `ea-bot`, in its deployed tree. | `run` only |
| `owner` | owner | A shell or a command as the owner, with the EA environment loaded. | `run` only |
| `signin` | owner | The owner role on the host's network, for sign-ins that redirect to `localhost`. | `run` only |

The owner reaches `ea-bot` only through `sudo -u ea-bot`, as on a host
install. `ea-bot` cannot read the owner's home, the owner's secrets or the
tokens once mediation is on.

## Requirements

- Docker Engine 24 or later with Compose v2. Buildx is optional;
  `docker compose build` works without it.
- About 1 GB of disk for the image (0.8 GB measured with Chromium).
- An amd64 or arm64 machine that stays on.

`setup` and `signin` run with `network_mode: host`, so the Microsoft
auth-code flow's redirect to `localhost:8400` reaches the container. On
Linux that is the host's own network. Docker Desktop (macOS, Windows)
supports host networking only when it is turned on: Settings, Resources,
Network, "Enable host networking" (Docker Desktop 4.34 or later). Without
it the container still reaches the internet, but a browser on your Mac
cannot reach its `localhost:8400`. The alternative the code allows is
the device-code flow: answer `device_code` for the Graph auth method (the
tenant must allow public client flows), which prints a link and a code
for any browser and needs no redirect; the Google sign-in already asks
you to paste the address back and needs no redirect either.

## Quick start

Copy the settings file and edit it: set `TZ` to your timezone (the cron
schedules use it) and `EA_PROFILE` to `email`, `review-only` or
`assistant`. `OWNER_UID` and `OWNER_GID` match your own (`id -u`,
`id -g`) when you want to read the volumes from the host.

```bash
cp deploy/docker/env.example .env
```

Then three commands:

```bash
docker compose build                 # 1. the image (a few minutes)
docker compose run --rm setup        # 2. the first configuration, once
docker compose up -d                 # 3. the stack
```

Step 2 asks the setup questions in your terminal and ends with the
doctor's checklist ([First run](#first-run-docker-compose-run---rm-setup)
says what it does). Two of its steps need you in a browser: the Claude
login for `ea-bot` and the Microsoft sign-in. Step 3 starts `init`, the
mediator and the scheduler; `docker compose --profile assistant up -d`
adds the chat bot.

To check the install again later:

```bash
docker compose run --rm owner python3 /opt/ea-system/scripts/ea-doctor.py --profile email
```

Use your own profile name. In a container the doctor also warns that
systemd is not available and that `crontab.master` is missing. Both
warnings are expected: compose replaces systemd, and supercronic
replaces the crontab.

## First run: `docker compose run --rm setup`

The `setup` service runs the entrypoint's `setup` role once, with a
terminal. It is the container's `/setup`: the host skill runs `sudo`, the
service-account and cron installers and the owner's own Claude Code,
none of which exist here, so the role does the same work with the users
the image already has. Each step runs as the user who owns what it
writes, the host install's split: `~/.ea` (the `ea-data` volume) is
ea-bot's space, mode 0750, so the config files and `~/.ea/.env` are
written as `ea-bot`; the secrets, the push settings and the tokens are
the owner's. The owner cannot create files in `~/.ea`, and ea-bot cannot
enter `~/.ea-private`. `init` runs first (the service depends on it), so
the volumes hold the config templates.

In order:

1. **The configuration questions**, as `ea-bot`: `scripts/install/user_init.py --phase
   <phase>` for `identity` (your name, email, role and the institution
   block: domains, the safety-rail domain, the video platform and your
   meeting link), `scheduling`, `inbox`, `autonomy`, `groups`,
   `deadlines`, `style`, `reimbursement` and `vip`, then `state`. Each
   writes one file under `~/.ea/config/`, mode 0640. Enter takes the
   default; a value still equal to the config template's placeholder
   (`Your Name`, the example role) is never offered as one. `tone` comes
   later, in step 6b: it reads your sent mail, which needs the mail
   token.

2. **`~/.ea/.env`**: the Microsoft Graph app registration (SETUP.md, Microsoft Graph app registration). The role asks the auth method (`confidential` or
   `device_code`), the application id, the tenant id (written as
   `GRAPH_AUTHORITY`) and, for a confidential client, the redirect URI
   and the client secret. The non-secret keys go in `~/.ea/.env`, written
   as `ea-bot` at mode 0640 so the owner's loader (`scripts/owner-io.sh
   env`) can take them through group `ea`; the secret goes in the
   owner-private `secrets.env` at mode 0600, never in `.env`. With no app
   registration yet, answer `skip` and have IT fill `docs/it-request.md`;
   run `setup` again when they answer.

3. **The chat values**, on the `assistant` profile only: Zulip (the
   server URL, the bot's email, your user id, the bot's API key into
   `secrets.env`; `scripts/zulip_bootstrap.py` prints them once the
   server runs, see [Self-hosted Zulip](#self-hosted-zulip)) or Slack
   (the channel id and name and your member id into `.env`, the two
   tokens into `secrets.env`; SETUP.md, Slack).

4. **The phone push**, on `email` and `review-only`, as the owner:
   `scripts/install/user_init.py --phase push` writes the owner-private `push.json`.

5. **The Claude login for `ea-bot`**, interactive: `claude auth login`
   (open the link it prints, sign in with the subscription the EA runs
   on, paste the code back), or `claude setup-token` for a long-lived
   token that the role stores in ea-bot's Claude settings (renew yearly;
   `docs/macos-claude-binary.md` describes the same token on macOS). The
   login lives in the `ea-bot-claude` volume.

6. **The Microsoft sign-in**, as the owner, interactive:
   `preprocessor/graph_client.py` with the settings of step 2. The device
   code flow prints a link and a code for any browser. The confidential
   flow prints a link to open on this machine and waits for the redirect
   on `localhost:8400`; the `setup` service runs on the host's network
   for that, like `signin`. After a confidential sign-in the role aligns
   the requested scopes to what the app grants (`--align --write`). The
   token lands in `~/.ea/auth` as the owner's file.

6b. **The tone phase**, right after a successful Microsoft sign-in:
   `scripts/install/user_init.py --phase tone` runs as the owner, who holds the mail
   token, and writes the style baseline from your sent mail to a
   directory only the owner can enter; `ea-bot` then writes it into its
   config as `writing-style-baseline.json`. With no sign-in the step is
   skipped, and the weekly style sync does the same work once the token
   is there.

7. **Google Calendar**, when `~/.ea/auth/google_client_secret.json` is
   already in place (SETUP.md, Google OAuth app); otherwise the role prints the
   command for later ([Mail and calendar sign-in](#mail-and-calendar-sign-in)).

8. **The install overlay**, rendered as `ea-bot` and installed in the
   deployed tree as the owner ([Install overlay](#install-overlay)).

9. **The doctor**, as the owner: `ea-doctor.py --profile <yours>
   --expect-missing <names>`. The role's exit status is the doctor's. The
   expected misses are the EA-Managed calendar (SETUP.md, Google OAuth app: create
   it in Google Calendar and put its id in `scheduling-preferences.json`;
   a warning on `email` and `review-only`) and whichever of the two
   logins you skipped. Everything else the doctor prints as a warning
   (systemd, `crontab.master`, the optional `EA_*` settings, the Google
   tokens, the deck builder) is the container's normal state; a FAIL
   names its fix.

Run `setup` again to change an answer: each phase shows the current
value as its default and rewrites its file, and a login that exists is
kept. Finish with `docker compose up -d`.

**Without prompts.** `--answers FILE` takes every answer from a JSON
file by key (`scripts/install/user_init.py --answers`; `docs/install-testing.md` lists
the keys, and the role adds `chat-frontend`, `zulip-site`,
`zulip-bot-email`, `zulip-owner-id`, `zulip-api-key`, `slack-owner-id`
and `claude-login`), asks nothing and skips the two logins, which need a
terminal. Mount the file where the argument names it:

```bash
docker compose run --rm -v ./answers.json:/answers.json:ro setup --answers /answers.json
```

`ea-bot` reads a copy of the file with the secret keys removed
(`graph-client-secret`, the chat tokens); the owner's steps read the
same copy and root writes the secrets. The install smoke runs the role
this way (`tests/install/docker-smoke.sh setup`, with
`tests/install/answers-email.json`).

## Volumes

| Volume | Mounted at | Holds |
|---|---|---|
| `ea-data` | `/home/ea-bot/.ea` | `~/.ea` of a host install: config, state, the ledger, the auth directory, plugins. |
| `ea-private` | `/home/<OWNER_USER>/.ea-private` | The owner's secrets (`secrets.env`), the plugin lock, logs and the generated crontab. Mode 0700. |
| `ea-bot-claude` | `/home/ea-bot/.claude` | ea-bot's Claude Code login, config file and session transcripts. |

`OWNER_USER` in `.env` (default `owner`) names the owner user in the
image; the same value sets its home and the mount point above.

The volumes outlive the containers. `docker compose down` keeps them;
`docker compose down -v` deletes them, and with them every token, the
ledger and the Claude login.

Use these named volumes, not bind mounts. The image holds each volume's
directories with their owners and modes (the ones `scripts/install/setup-ea-user.sh` sets
on a host), and Docker copies them into a new named volume. `init` stops
with a message when a volume belongs to the wrong user, rather than chown
a tree that `ea-bot` can change.

The code is in the image, not in a volume: `/opt/ea-system` for the
owner's jobs and services (the bot, the mediator, the scheduler and the
`owner` role all run from it; its parent is root's), and
`/home/ea-bot/ea-system` for the Claude sessions the chat lanes start.
Both belong to the owner and no one else can write them. No owner
process runs code from the copy in ea-bot's home, whose parent ea-bot
owns (`docs/SECURITY.md`, owner services off the ea-bot tree).

## Claude login

`ea-bot` needs its own Claude login. The `setup` role asks for it; to do
it again later, run Claude Code as `ea-bot`:

```bash
docker compose run --rm agent claude auth login
```

Follow the link it prints and paste the code back. The login is kept in
the `ea-bot-claude` volume.

For a login without a browser on this machine, make a long-lived token
instead:

```bash
docker compose run --rm agent claude setup-token
```

Then put the token in ea-bot's Claude settings as
`CLAUDE_CODE_OAUTH_TOKEN`, the way `docs/macos-claude-binary.md`
describes for macOS (the `setup` role does this when you choose the
token).

The image pins Claude Code by version and checksum, and the first start
turns the self-updater off in ea-bot's settings. To move to a new version,
see [Upgrade](#upgrade).

## Config

The `init` service copies the config templates into `ea-data` on the
first start and writes `EA_PROFILE` into the new `ea-config.json`. Later
changes to the profile go in `ea-config.json`.

To replace a config file, copy it in and start again:

```bash
docker compose cp ./user-profile.json mediator:/home/ea-bot/.ea/config/user-profile.json
docker compose up -d
```

`init` runs on every `up`. As `ea-bot`, it rewrites a copied-in config
file (or `~/.ea/.env`) as an `ea-bot` file, mode 0640, readable by group
`ea`.

Secrets go in `secrets.env` in the `ea-private` volume, not in `.env`:

```bash
docker compose cp ./secrets.env mediator:/home/owner/.ea-private/secrets.env
docker compose up -d
```

`init` makes `secrets.env`, `bot.env` and `crontab.extra` the owner's,
mode 0600.

## A step by hand

The `setup` role is the normal path. Each of its steps is also one
`docker compose run`, for a change later or a step it skipped; the user
each runs as is the one who owns the file, the split
[First run](#first-run-docker-compose-run---rm-setup) describes.

- **A config phase**, as `ea-bot` through the `agent` service:

  ```bash
  docker compose run --rm agent python3 /home/ea-bot/ea-system/scripts/install/user_init.py --phase scheduling
  ```

  The phases are `identity`, `scheduling`, `inbox`, `autonomy`,
  `groups`, `deadlines`, `style`, `reimbursement`, `vip`, `state` and
  `summary`; `push` runs as the owner (`owner` in place of `agent`,
  `/opt/ea-system/scripts/install/user_init.py`). `auth` stays out of the container: it
  signs in as the user that runs it, and run as ea-bot it would put the
  client secret in ea-bot's own private directory; the `setup` role's own
  `.env` step and the owner's sign-in replace it. A finished
  `user-profile.json` can be copied in instead:

  ```bash
  docker compose run --rm -T agent sh -c 'umask 027; cat > /home/ea-bot/.ea/config/user-profile.json' < ./user-profile.json
  ```

- **`~/.ea/.env`**, the non-secret settings (`GRAPH_AUTH_METHOD`,
  `GRAPH_CLIENT_ID`, `GRAPH_AUTHORITY`, `GRAPH_REDIRECT_URI`,
  `GRAPH_SCOPES`, `EA_MODEL`, the `EA_SLACK_*` ids; the timezone comes
  from `TZ` in compose's `.env`), written as `ea-bot`, readable by group
  `ea` so the owner's loader can take its config keys:

  ```bash
  docker compose run --rm -T agent sh -c 'umask 027; cat > /home/ea-bot/.ea/.env' < ./ea.env
  ```

  The loader (`scripts/owner-io.sh env`) takes only the config keys
  from this file; a secret put here is skipped with a warning.

- **`secrets.env`**, the owner-private secrets (`GRAPH_CLIENT_SECRET`,
  `ZULIP_API_KEY`, the Slack tokens), written as the owner with mode
  0600:

  ```bash
  docker compose run --rm -T owner sh -c 'umask 077; cat > "$HOME/.ea-private/secrets.env"' < ./secrets.env
  ```

- **The sign-ins**, as the owner
  ([Mail and calendar sign-in](#mail-and-calendar-sign-in)): the
  `signin` service for the auth-code flow, the `owner` service for the
  device-code flow and for Google.

Then `docker compose up -d`: `init` adopts a copied-in file as ea-bot's
(mode 0640) and renders the install overlay from the new config, and
each service installs it at start.

## Install overlay

`CLAUDE.md` names no person and no institution. The install's own facts
(your name, the institution block of `user-profile.json`, the chat
streams, the sites and lists) live in the install overlay, which
`preprocessor/install_overlay.py` renders from the config. Claude Code
loads it as `CLAUDE.local.md` beside `CLAUDE.md` in the deployed tree.

The entrypoint does both steps at start, after `init` has put the config
in place:

1. `render`, as `ea-bot`: writes `~/.ea/config/CLAUDE.install.md` in the
   `ea-data` volume. `init` does this on every `up`.
2. `install --tree /home/ea-bot/ea-system`, as the owner, who owns the
   tree: copies it there as `CLAUDE.local.md`.

The tree is in the image, not in a volume, so `bot`, `agent` and `owner`
render again and install their own copy each time they start. After you
change `user-profile.json` or another config file, run `docker compose up
-d` and `docker compose restart bot`. A failed step is logged and the
service still starts; the chat prompts then carry the overlay
themselves.

## Mail and calendar sign-in

Sign-ins run as the owner, so the tokens belong to the owner from the
start.

- **Microsoft Graph, auth-code flow** (the confidential client most
  universities provision). The flow waits for the browser's redirect on
  `localhost:8400`, so run it on the host's network:

  ```bash
  docker compose run --rm signin
  ```

  Open the link it prints in a browser on this machine.

- **Microsoft Graph, device-code flow** (where the tenant allows it):

  ```bash
  docker compose run --rm owner python3 /opt/ea-system/preprocessor/graph_client.py
  ```

- **Google accounts.** The setup prints a link, and you paste the address
  your browser lands on back into the terminal:

  ```bash
  docker compose run --rm owner python3 /opt/ea-system/preprocessor/google_calendar_client.py --setup
  ```

The sign-ins write the token files as the owner (mode 0600, or 0660 in
the owner's own group), where the mediator reads them and `ea-bot` cannot. Sign in again in the container rather than copying
token files in from a host install: a copied file arrives as root, and
`init` does not change files in the auth directory.

## Profiles and cron jobs

The scheduler writes its crontab at every start with
`deploy/docker/crontab.py`, from the profile in `ea-config.json`:

- **Every profile:** the morning briefing, the afternoon update, the weekly
  audit, intake, ledger sync, the closure scrubber, the calendar
  reconciler and the weekly style sync.
- **assistant:** also the scheduling and priority checks, deferred sends,
  the quiet-hours flush and the monthly style analysis.
- **Admissions filter and digest:** when `ea-config.json` enables
  `admissions_filter`.
- **Plugins:** one line per schedule of each plugin in `~/.ea/plugins/`.
  A v1 plugin's own `plugin.json` decides; a v2 plugin is scheduled from
  the owner's lock (`plugins.lock.json` in the `ea-private` volume), so
  it needs an enabled, accepted entry there first:
  `docker compose run --rm owner python3 /opt/ea-system/scripts/ea-plugin.py install git:<url> --accept-capabilities`
  (`docs/plugins.md`). Owner plugins' cron jobs are the lines
  `ea-plugin.py cron-lines` prints.

To add your own lines, put them in `crontab.extra` in the `ea-private`
volume, then run `docker compose up -d` and `docker compose restart
scheduler`. The scheduler uses the file only when it belongs to the owner
and no one else can write it; `init` sets that.

Not in the container: the nightly evals (they read git history) and the
browser token harvests of a host install.

## Self-hosted Zulip

Set the five `ZULIP__*` secrets in `.env` (`openssl rand -hex 32` makes
each one), then:

```bash
docker compose --profile zulip --profile assistant up -d
```

The browser on this machine reaches Zulip at
`http://zulip.localhost:8090`. The bot reaches it at
`http://zulip.localhost`, a network alias inside compose. Point the bot's
Zulip config at that address. `deploy/zulip/README.md` covers creating the
realm, the bot and the streams.

## Upgrade

```bash
git pull
docker compose build
docker compose up -d
```

The volumes keep the state, the config and the logins. To move Claude Code
to a new version, set `CLAUDE_CODE_VERSION` in the Dockerfile and the two
`CLAUDE_CODE_SHA256_*` values from that version's release manifest
(`https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`).
To move a Python package, edit `deploy/docker/requirements.top`, run
`bash deploy/docker/lock.sh` and rebuild.

## Differences from a host install

- **No systemd.** Restart the bot with `docker compose restart bot`. A
  runbook step that runs `systemctl restart ea-chat-bot` maps to this.
- **No deploy hook.** A commit does not change a running container. Build
  and `up` again.
- **No resident pane.** Chat runs one Claude session per topic, the
  default. The tmux resident is not started.
- **Debian packages** come from the trixie archive at build time. The base
  image, the Python packages, supercronic and Claude Code are pinned.

## Stage 2 (not built)

Stage 1 keeps a sudo rule inside the image: the owner may run anything as
`ea-bot`. Stage 2 replaces it with a runner service. It would run as
`ea-bot` in its own container and take turn requests from the bot over a
Unix socket on a shared volume. The bot and the mediator would then have
no way to start `ea-bot` processes directly, and the agent container would
have no sudo at all. Build it only after stage 1 has run a full week.
