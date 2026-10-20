# Install testing

The install is tested in three tiers. The first two need no account of
any kind and run in GitHub Actions (`.github/workflows/ci.yml`); the
third needs a Claude login and runs on the owner's machine.

| Tier | What it proves | Where | Needs |
|---|---|---|---|
| 1, the unit wall | every `tests/test_*.py` passes on a clean machine with no install to read | the `wall` job: ubuntu-22.04 (Python 3.10, the floor), ubuntu-latest, macos-latest | Python, `requirements.txt`, pytest, PyYAML |
| 2, the install smoke | `user_init.py`, `setup.sh`, the service-account installer, the overlay and the doctor run end to end on a fresh machine and leave only the misses no account can fill | `install-smoke-linux` (ubuntu-latest), `install-smoke-macos` (macos-latest); `tests/install/docker-smoke.sh` locally | passwordless sudo, the network, no `~/.ea` |
| 3, the release suite | the models the lanes run on today answer as well as last time | the owner's machine, Saturday night and when a tag is cut | a Claude login, the remote-LLM switches |

The `lint-and-validate` job (the secrets lint, the config templates, the
syntax of the bot and the preprocessor) runs beside them on every run.

## Tier 1: the wall

`python3 tests/run_wall.py` runs every test file, then `bash
tests/secrets_lint.sh` and `python3 tests/validate_config.py --ci`. The
job points `HOME` at an empty directory and sets `EA_DIR=/nonexistent`
before the wall runs, so a test that reads a real install fails here
rather than passing on the owner's machine. The wall needs `pytest` and
`PyYAML` beyond `requirements.txt`; the job installs both.

Every push to `main` and every tag runs the full tiers, macOS included
(standard runners are free on a public repository). A pull request runs
only the lint unless it carries the `full-ci` label (adding the label
starts the run) or is run by hand with Actions > CI > Run workflow; the
wall legs start, skip their steps and report, and the `ci` job at the
end passes when every job passed or was skipped; branch protection
requires that one check, and a pull request need not be current with
`main` to merge, since every push to `main` runs the full tiers. A
draft runs nothing, and a new push cancels the pull request's run in
progress. A first pull request from a fork waits for a maintainer to
approve its workflow run.

The front door, `install.sh`, is in the wall too (`tests/test_install_sh.py`):
`bash -n`, `--check` on the host and on a bare PATH (git and python3
reported missing, exit 1), and the whole pre-launch path with
`--no-launch` from a temporary copy of the checkout, run as a file and
piped on stdin, with a stub `claude` and a stub `python3` on PATH. The
stub claude logs every call, so the test proves no session starts and
nothing is installed; a grep keeps the script free of bash 4 constructs,
since macOS ships bash 3.2.

## Tier 2: the install smoke

`tests/install/smoke.sh` is the fresh-install path a new user follows,
with nothing read from the terminal:

1. The config, from `tests/install/answers-email.json`:
   `python3 user_init.py --answers <file> --phase <phase>` for each
   config phase (`identity`, `scheduling`, `inbox`, `autonomy`, `groups`,
   `deadlines`, `style`, `auth`, `reimbursement`, `vip`, `push`,
   `state`). `tone` reads sent mail and `summary` only prints, so neither
   runs. `auth` writes the Graph keys for the device-code flow and skips
   the sign-in.
2. `EA_NONINTERACTIVE=1 ./setup.sh --profile email`: the templates, the
   profile, the cron template. The flag (or `--non-interactive`, or a
   standard input that is not a terminal) skips the Graph and Google
   sign-ins, printing the command for each, and saves the cron entries to
   `~/.ea-private/crontab-template.txt` instead of installing them
   (`--install-cron` installs them).
3. `sudo bash setup-ea-user.sh` (Linux) or `sudo bash
   setup-ea-user-macos.sh` (macOS): the `ea-bot` user, group `ea`, the
   deployed tree, `~/.ea` moved into ea-bot's home, the sudoers rule,
   Claude Code for ea-bot (downloaded on a runner; already in the Docker
   image).
4. The install overlay: `install_overlay.py render` as ea-bot,
   `install --tree` as the owner (SETUP.md "The install overlay").
5. `scripts/ea-doctor.py --profile email --expect-missing <names>`. The
   smoke exits with the doctor's status.

The overlay install and the doctor run through `sudo -u <owner>` so the
process carries the `ea` group the installer added; the shell that
started the smoke predates it, as the user's own shell does until they
log in again (SETUP.md "After the installer").

On macOS the smoke uses the Homebrew `python3` (`/opt/homebrew/bin`,
else `/usr/local/bin`) for every owner-side step and for the render as
ea-bot: Apple's `/usr/bin/python3` is 3.9, below the floor, and a
toolcache interpreter under the runner's home is out of ea-bot's reach
once the installer closes that home. The `install-smoke-macos` job
installs it with Homebrew first. No macOS machine was available when
this was written; the macOS branches are small and marked in the script.

Settings: `EA_SMOKE_PROFILE` (default `email`), `EA_SMOKE_ANSWERS`,
`EA_SMOKE_EXPECT_MISSING`, `EA_SMOKE_PYTHON`.

The smoke refuses to run as root or when `~/.ea` exists: it installs
into the invoking user's home and must never meet a live install.

### Expected misses

`ea-doctor.py --expect-missing NAME,NAME,...` exits 0 when every FAIL
starts with one of the names. A name that matches no FAIL is reported
(`expected_missing_unmatched` in `--json`, "did not fail" in the text)
and changes nothing. The smoke names these three:

| Check name | Why it cannot pass with no accounts | Profile |
|---|---|---|
| `ea-bot: Claude login` | ea-bot has run no `/login`; `.claude/.credentials.json` and the settings token are both absent | every profile |
| `auth: msal_token_cache.json` | no Graph sign-in has happened, so the token cache does not exist | every profile |
| `EA-Managed calendar` | `scheduling-preferences.json` names no `ea_managed_calendar.calendar_id` | a FAIL on `assistant`; a WARN on `email` and `review-only`, where naming it is harmless |

A prefix stands for a family: `config:` covers every `config: <file>`
check. Other names the doctor can print on a bare machine, for an
`EA_SMOKE_EXPECT_MISSING` of your own: `ea-bot: .local/bin/claude` (the
Claude Code download for ea-bot did not finish), `ea-bot user`,
`owner in group ea`, `dir: ~/.ea/<name>`, `profile: name and email`,
`~/.ea/.env`, `env: <KEY>`, `secret: <KEY>`, `service: <unit>`,
`crontab`. Everything the smoke leaves as a WARN (systemd absent in a
container, `crontab.master`, the optional `EA_*` settings, the Google
tokens, the deck builder) is the normal state of a new install.

`--probe-codex` runs `python3 preprocessor/codex_cli.py probe` when that
script exists and reports `codex probe` as ok (exit 0) or a WARN (any
other exit, or no script). It never adds a FAIL: codex is optional.
`EA_CODEX_CLI` names another script to probe; the tests use it.

### The answers file

`user_init.py --answers FILE` takes every answer from a JSON object
keyed by prompt and never calls `input()`. A key left out takes the
prompt's default (what Enter gives), an empty value the same, and a
required prompt with no default and no key stops the run and names the
key. A yes/no answer is a JSON boolean or a string starting with `y`; a
list answer is a JSON list or a comma-separated string; a loop
(`groups`, `deadlines`) takes one entry from a file, since its prompts
share their keys. Keys the run never asked for are listed at the end of
a full run. The keys, by phase:

| Phase | Keys |
|---|---|
| identity | `full-name`, `work-email`, `role-description`, `department`, `university`, `office`, `slack-channel-name`, `bio-blurb`, `calendar-accounts`, `institution-name`, `institution-domains`, `institution-related-domains`, `institution-safety-rail-domain`, `video-platform`, `video-link` |
| scheduling | `timezone`, `cluster-meetings`, `default-duration-minutes`, `vip-end-time`, `google-calendars` |
| inbox | `inbox-zero`, `archive-as-completion`, `stale-thread-days` |
| autonomy | `conservative-defaults`, `auto-promote-threshold`; with `conservative-defaults` false, `tier-draft-email-response`, `tier-send-scheduling-availability`, `tier-upload-letter-of-recommendation`, `tier-prep-reimbursement-email`, `tier-create-calendar-hold`, `tier-archive-organize-inbox`, `tier-update-obligations-ledger`, `tier-post-slack-summary` |
| groups | `groups-configure`, `group-name`, `group-addresses`, `group-add-another` |
| deadlines | `deadlines-configure`, `deadline-domain`, `deadline-category`, `deadline-add-another` |
| style | `style-default`, `style-internal`, `style-external-academic`, `style-student` |
| auth | `graph-auth-method` (`1`-`4`, or `confidential`, `device_code`, `interactive`, `skip`), `graph-client-id`, `graph-tenant-id`, `graph-redirect-uri`, `graph-client-secret`, `graph-sign-in-now`, `google-calendar-now`, `slack-bot-token`, `slack-app-token`, `slack-channel-id`, `slack-channel-name-env`, `slack-owner-id` (the Slack keys only on the `assistant` profile; the secrets go to `~/.ea-private/secrets.env`) |
| tone | `style-analysis-now` |
| reimbursement | `reimbursement-configure`, `reimbursement-first-name`, `reimbursement-last-name`, `reimbursement-email`, `reimbursement-affiliation`, `reimbursement-form-url` |
| vip | `vip-addresses` |
| push | `push-configure`, `push-ntfy-url`, `push-topic`, `push-include-text`, `push-click` |

`tests/install/answers-email.json` is a complete `email` install: a
made-up person at `example.edu`, an institution block, zone
`America/Chicago`, no chat platform, no remote model, Outlook by device
code with the sign-in deferred, and an ntfy push. The identity phase now
asks the institution questions `/setup` section 2b asks, and the `push`
phase writes `~/.ea-private/push.json` (mode 600) the way SETUP.md
"Phone push" describes.

### Running the smoke locally, in Docker

```bash
docker build -t ea-system:worktree-check .     # once; or any EA image
bash tests/install/docker-smoke.sh              # EA_SMOKE_IMAGE picks another image
```

The wrapper starts a container from the image, removes the image's
prepared `/home/ea-bot/.ea` and `/home/ea-bot/ea-system` (a host has
neither before the installers run), adds a fresh owner user with
passwordless sudo, copies this tree in as a git repository in that
user's home, and runs `smoke.sh` as that user. The image already holds
Claude Code for ea-bot, so that download does not happen; `setup.sh`
still reaches the package index. Without systemd the doctor warns on
`systemd` and the mail-mediation step of the installer reports a
warning; neither is a FAIL (docs/docker.md says the same of the
container). The container is removed at the end whatever the result,
and no volume is created.

Nothing in the smoke touches the machine it runs on outside the
container; never run `smoke.sh` itself on a machine with a live install.

## Tier 3: the release suite

The release tests (`tests/release/run_release.py`) send every case to a
model: the briefing fixtures to the bot's Claude default, the drafting
cases to the remote writer or Claude, the scheduling cases to the
scheduling pipeline or the remote agent, then a pinned judge scores
them. They need a Claude login and the remote-LLM switches, so they run
on the owner's machine: Saturday at 23:00 from `crontab.master`, and
when `scripts/cut-tag.sh` cuts a tag. docs/EVAL.md, "Release tests",
has the lanes, the verdicts (`green`, `amber`, `red`, `harness`) and the
exit codes.

## Cost notes

- Standard runners are free on a public repository, so pushes to `main`
  and tags run everything. Pull requests still run only the lint unless
  labelled `full-ci`, to keep the queue short and the wall a local habit;
  the macOS smoke runs on tags and Run workflow only.
- The Linux smoke downloads Claude Code for ea-bot and the Python
  packages on every run (a few minutes). It starts no service that
  outlives the job; the mediator unit the installer starts dies with the
  runner.
- There is no Windows leg: the install needs a Unix service account, and
  SETUP.md names WSL2 for Windows machines, which the ubuntu legs cover.
- The wall's ubuntu-22.04 leg pins Python 3.10 so the floor SETUP.md
  states stays true; the other legs take a current release.

## What a bare runner cannot test

The tests of the owner-versus-resident boundary need the two-user layout:
an `ea-bot` user and passwordless sudo to it from the user running the
tests. On a GitHub runner neither exists, so those tests skip there
(`tests/conftest.py resident_child_available`, the reason names the
layout) and run in two places that have it: the `wall-docker` job, which
runs the whole wall inside the product's own image, and any installed
host. The suite runs in the zone its calendar and scheduling tests
state their clocks in (`tests/conftest.py SUITE_TZ`, set by conftest and
by `run_wall.py` when the host sets none), and the wall jobs give the tests an
empty directory as `EA_DIR` and `HOME`; a path that cannot be made fails
the tests that build their own install there. Linux legs install
`poppler-utils` so the PDF fallback test can name a real binary.
