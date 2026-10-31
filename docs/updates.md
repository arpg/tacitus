# Updates

An install keeps itself current the way Claude Code does: a small job
checks for a new release every hour, installs it when nothing is running,
and the user sees one line about it in the next morning briefing. There
is no update button and no rollback command to learn. A release that does
not come up healthy is switched back by the same job, and the version it
refused is held back until a newer one appears.

This page is the design and the operator's reference. The pieces:

| Piece | Where |
|---|---|
| The release channel: a signed tag `vX.Y.Z` on every green push to `main` | `.github/workflows/ci.yml`, job `release` |
| The release notes: a GitHub Release per tag, and what an install records of it | `scripts/release_notes.py` |
| The trust root: the public half of the signing key, pinned on each install | `.release-key.pub`, then `release_key` in `~/.ea-private/update.json` |
| The `stable` channel: a tag the maintainer moves | `scripts/cut-tag.sh --stable` |
| The updater on an install | `scripts/update.py` |
| The release layout under `/opt` (made once, as root) | `deploy/harden-permissions.sh`, `deploy/macos/install-services.sh` |
| The timer | `tacitus-update.timer` (Linux), `<prefix>.tacitus-update` LaunchDaemon (macOS) |
| What the user sees | the doctor's `updates` row, one briefing line, the `version` chat command |
| The relay host | `relay/tacitus-relay-update.timer`, `scripts/update.py relay` |

## The channel

### Release tags

Every push to `main` runs the full CI tiers. When they pass, the `release`
job (after the `ci` summary job, on pushes to `main` only) creates an
annotated tag on that commit and pushes it:

- The version is the newest `vX.Y.Z` tag in `main`'s history with the
  patch number raised by one. `#minor` or `#major` in the released commit
  (the merge or squash commit) or in the body of the pull request it
  merged, or a footer line `release: minor` (or `major`) there, raises
  that part instead and resets the parts below it. A marker is a whole
  token between spaces or line ends: `#minor,` or a quoted `` `#major` ``
  raises nothing. Older commits are never read, so a message that only
  mentions a marker cannot raise a later release. The marker goes in the
  pull request's title or body.
- A commit that a newer release already contains gets no tag: two pushes
  in quick succession can finish out of order, and the older one must
  never get the higher number. The job runs in a concurrency group of its
  own, so two releases never compute the same number.
- The tag is signed with an SSH key held as the secret
  `RELEASE_SIGNING_KEY` of the `release` environment: `git -c
  gpg.format=ssh -c user.signingkey=<file> tag -s`, in the one step that
  sees the secret ("Supply chain", below). Before it pushes, the job
  checks that the secret's public half is the committed
  `.release-key.pub` and verifies its own tag with it.
- The lightweight tag `channels/latest` moves to the same commit. It is a
  pointer for people and dashboards; the updater does not trust it.
- With no secret set the job prints a warning and tags nothing, so a fork
  or a repository before the one-time setup stays green.
- Once the tag is pushed, the job creates a GitHub Release for it whose
  body is the release notes (below), with `gh release create --verify-tag
  --notes-file`. A Release that cannot be made is a warning and the job
  stays green: the signed tag is the release, and the notes describe it.

A tag pushed with the workflow's own token starts no workflow, so a
release tag does not run CI a second time; the commit it names has just
passed the tiers. The macOS tiers that run on tags run when the
maintainer pushes a tag by hand or uses Run workflow.

A hand-cut tag (`scripts/cut-tag.sh vX.Y.Z`) is not signed, and installs
skip it. To move the minor or major number, put the marker in the merge
commit instead.

### Release notes

`scripts/release_notes.py` writes the notes from the history between the
previous release and the new tag, in merge order:

- **Changes**: one line per first-parent commit. A merge commit gives the
  pull request's title (the line after `Merge pull request #N from ...`),
  a squash commit its subject without the trailing `(#N)`, a direct
  commit its subject. A title states the mechanism in one line
  (CONTRIBUTING.md), so the line is the change. Each line links its pull
  request and names the commit.
- **Owner steps**, when there are any: each line that starts with
  `Owner-steps:` in a commit message of the range (the merge or squash
  commit and the commits a merge brings in) or in the message of the
  annotated tag. Never a pull request body or a Release body: those can
  change after the merge, and the briefing hands each step to the owner
  as an instruction. The line names one step; with nothing after the
  colon, the list items under it are the steps. Fenced code is skipped,
  and `none` is no step.

The previous release is the newest `vX.Y.Z` tag in the new tag's history
below it; with none, the tag is the first release and every commit in its
history is a change.

```
python3 scripts/release_notes.py v1.2.2..v1.2.3          # Markdown, the Release body
python3 scripts/release_notes.py --tag v1.2.3 --json     # {"tag", "previous", "changes": [{"subject", "pr", "sha"}], "owner_steps": [...]}
python3 scripts/release_notes.py --tag HEAD              # what the next release would say
```

An install records the notes when an update comes up healthy (step 13).
The list of changes is the body of the tag's GitHub Release, read with
the API, when that release starts at the version installed before;
otherwise, when the API does not answer or the install skipped a release,
it is made again from the history in the updater's cache since the
version installed before. The owner steps always come from that history,
the commit messages between the installed version and the verified tag
and the tag's own message; the Release body is shown, never trusted. The
status file keeps them under `notes` (`tag`, `previous`, `notes_url`,
`count`, `changes`, `owner_steps`, `source`), and the notes of the ten
releases installed before under `notes_history`.

### The trust root

`.release-key.pub` holds one or more `ssh-ed25519` public keys, one per
line. Each install pins them outside every release tree: `release_key`
in the owner-private `~/.ea-private/update.json`, a list of key lines (or
the absolute path of a key file the owner or root owns and no one else
can write). The root step (`update.py layout`) writes it once, from the
first installed release's `.release-key.pub`; an install made before the
pin writes it on its next run, from the release it runs then. After that
no release changes it: `apply` never rewrites it. The updater builds an
allowed-signers file from the pin with the principal `tacitus-release`
and the namespace `git`, and accepts a tag only when all of these hold:

1. It is an annotated tag, and the name inside the tag object is the name
   of the ref. A ref `v9.9.9` that points at the signed object of
   `v0.1.3` is refused, so an old release cannot pose as a new one.
2. It carries an SSH signature (`-----BEGIN SSH SIGNATURE-----`); a PGP
   block is refused before git sees it.
3. `git -c gpg.ssh.allowedSignersFile=<file> verify-tag` exits 0 and says
   `Good "git" signature for tacitus-release with ED25519 key`.
4. The commit it names is in the history of the repository's `main`,
   fetched beside the tags (`git merge-base --is-ancestor <commit>
   origin/main`).
5. The release's own `.release-key.pub` names no key the pin lacks.

A key rotation is a release signed by the old key whose
`.release-key.pub` names the new one. Installs refuse it (condition 5),
log it, and the doctor's `updates` row warns, until the owner checks the
new key against the maintainer's announcement and adds its line to
`release_key`; the install stays on the newest release the pin covers.
A release that only drops a key installs, and the pin keeps trusting the
dropped key until the owner removes its line. A key that leaks is
replaced the same way: the owner removes the leaked line.

### Channels

| Channel | Follows | Default for |
|---|---|---|
| `latest` | the newest signed `vX.Y.Z` tag | the `assistant` profile |
| `stable` | the signed `vX.Y.Z` tag at the commit `channels/stable` points to | `email`, `review-only` |
| `off` | nothing; the install stays where it is | |

`channels/stable` is an annotated tag the maintainer moves with
`scripts/cut-tag.sh --stable [vX.Y.Z] [--push]`. It runs the release tests
(docs/EVAL.md, "Release tests") on HEAD, which must be that release, and
moves the pointer only on a green or amber verdict. The pointer itself is
not signed: it can only choose among signed releases, and the updater
never moves an install backwards (below).

The channel is `update.channel` in `~/.ea/config/ea-config.json`: `auto`
(the profile's default, the value a new install's template carries),
`latest`, `stable` or `off`. `~/.ea/config` is the EA user's to write, so
the owner can pin the value where the EA cannot reach it:
`~/.ea-private/update.json` with `{"channel": "off"}` (or any channel)
outranks the config file. The repository the updater reads is fixed in
the code (`https://github.com/arpg/tacitus.git`); only the owner-private
file can change it (`"repo": "<url>"`), never ea-config.json.

## The updater

`scripts/update.py` uses the standard library and git only. It runs as the
owner, never as root and never as the EA user.

```
python3 /opt/ea-system/scripts/update.py status            # what is installed, what the channel offers
python3 /opt/ea-system/scripts/update.py check             # fetch tags, verify, say whether an update is due
python3 /opt/ea-system/scripts/update.py apply --dry-run   # everything up to the swap, in a scratch directory
python3 /opt/ea-system/scripts/update.py apply [--tag vX.Y.Z]
python3 /opt/ea-system/scripts/update.py run               # what the timer runs: apply on the channel, quiet when current
python3 /opt/ea-system/scripts/update.py verify            # the health gate, on what runs now
python3 /opt/ea-system/scripts/update.py rollback          # back to the previous release by hand; holds the current one back
```

### The release layout

```
/opt/ea-system                  -> ea-system.releases/current     (root's link, made once)
/opt/ea-system.releases/        owner:ea 0750
    current -> v0.1.7           the owner's link, switched with rename()
    previous -> v0.1.6
    v0.1.6/  v0.1.7/            each a `git archive` of a verified tag, with .version and .deployed-commit
```

`/opt` belongs to root, so the owner can create nothing beside
`/opt/ea-system`. The root step (`deploy/harden-permissions.sh` on Linux,
`deploy/macos/install-services.sh` on macOS, through
`scripts/update.py layout`) creates the release directory once and puts
the root-owned link in place of the code tree. From then on every switch
happens inside the owner's release directory: a new link named
`.current.new` is renamed over `current`, which is atomic. A process
that is running keeps the files it started from, since the release it
came from stays on disk as `previous`.

The root step converts a tree only when an update channel is on: a new
install (no `/opt/ea-system` yet, and a template `auto`), or an existing
one whose `update.channel` (or private `update.json`) names `latest`,
`stable` or `auto`. A tree the post-commit hook writes and whose config
has no `update` key stays a plain directory, and the updater leaves it
alone. That is the maintainer's machine: commits on `main` in the
checkout deploy through the hook, and nothing updates them from GitHub
behind it. Once the layout exists the hook in turn leaves `/opt` and the
EA user's copy alone ("deploy skipped: managed by the updater").

### One update, step by step

1. **Lock.** One updater at a time (`~/.ea-private/update/update.lock`).
2. **Fetch.** The tags (`v*` and `channels/*`) go into an owner-private
   bare cache, `~/.ea-private/update/tacitus.git`.
3. **Pick.** The channel's newest signed release. It must be newer than
   the installed one (a lower or equal version is never installed), and
   not the version held back after a failed gate.
4. **Verify** the tag as above, against the pinned `release_key`: the
   signature, the commit in `main`'s history, and a key list the pin
   covers. A tag that fails is skipped and the next older one is tried.
5. **Stage.** `git archive` of the tag's commit into
   `/opt/ea-system.releases/.staging-<tag>`, with `.version` (the tag) and
   `.deployed-commit` (the short commit) beside the code.
6. **Smoke.** `python3 tests/validate_config.py --ci` in the staged tree,
   and every entry point compiles: `ea-chat-bot.py`, `run-agent.sh`
   (`bash -n`), `scripts/update.py`, `scripts/ea-doctor.py` and
   `preprocessor/*.py`. A failure deletes the stage and holds the version
   back.
7. **Wait for idle.** No bot dispatch in flight (`~/.ea-private/bot-inflight.json`,
   the lanes other than `resident` and `deferred`, the rule the deploy
   hook uses) and no `run-agent.sh` or `run-plugin.sh` of the owner
   running. Up to ten minutes; still busy, the stage is deleted and the
   next timer run tries again.
8. **The EA user's copy.** `scripts/deploy.sh --from <cache> --ref
   refs/tags/<tag>` moves `<ea-bot home>/ea-system` to the same commit,
   with the deploy hook's checks on that tree.
9. **Swap.** `current` now names the new release; `previous` the old one.
10. **Restart** the bot, through the sudoers grant the deploy hook uses
    (`sudo -n systemctl restart ea-chat-bot.service`, or `launchctl
    kickstart -k` on macOS), and the mail mediator (a signal; its unit
    restarts it). Every update restarts both, unlike the hook, which
    restarts only for code held in memory: each service resolves its own
    path when it starts, so it keeps reading the release it started from,
    skills and `CLAUDE.md` included, until it restarts. The release before
    stays on disk as `previous` for the same reason. The idle wait in
    step 7 is what makes the restart free.
11. **Health gate, 60 seconds.** The service is active with the same main
    process throughout, its log (`~/.ea-private/logs/slack-bot.log`, and
    the journal or the launchd log) shows the chat frontend connected and,
    when the instance before it served the web chat, `web chat serving
    on`, and no `Traceback` and no `refused to start`. The mediator passes
    on its process alone (and its journal on Linux). With no log to read,
    a process that stays up 20 seconds passes. On an install with no
    service to restart the gate is the smoke alone.
12. **Rollback**, when the gate fails: `current` goes back to the old
    release, the EA user's copy back to its old commit, the services
    restart again, and the version is recorded as held back. It stays
    held back until a newer release appears or `apply --tag` names it.
13. **Record.** `~/.ea-private/update/status.json` (the owner's copy) and
    `~/.ea/state/update-status.json` (the copy the EA reads, written as
    the EA user), with the new release's notes ("Release notes", above).
    Releases older than `previous` are deleted, except the three newest.

### Where it does nothing

- `update.channel` is `off`, or the tree is the hook's plain directory
  with no channel set: `status` says so and `run` exits 0.
- In a container (`/.dockerenv`): the code is in the image. `check` still
  reports, and `apply` says to rebuild: `git fetch --tags && git checkout
  vX.Y.Z && docker compose build && docker compose up -d`
  (docs/docker.md, "Upgrade").
- No `release_key` in `update.json` and no `.release-key.pub` in the
  installed release to pin: nothing can be verified, so nothing is
  installed.

## The timer

**Linux.** `deploy/harden-permissions.sh` writes two system units when the
release layout exists, and enables the timer:

```ini
# /etc/systemd/system/tacitus-update.service
[Service]
Type=oneshot
User=<you>
Group=<your group>
SupplementaryGroups=ea
Environment=HOME=/home/<you>
ExecStart=<the services' interpreter> /opt/ea-system/scripts/update.py run
Nice=10
TimeoutStartSec=45min

# /etc/systemd/system/tacitus-update.timer
[Timer]
OnCalendar=hourly
RandomizedDelaySec=55min
Persistent=true
```

They are system units for the reason the bot's unit is one: a user unit
keeps the groups its user manager started with and cannot write the EA's
group-`ea` state. `journalctl -u tacitus-update` has each run.

**macOS.** `deploy/macos/install-services.sh` writes the LaunchDaemon
`<prefix>.tacitus-update`: `StartInterval` 3600, `run --jitter 900` (the
updater waits up to 15 minutes first, since launchd has no randomized
delay), output in `~/.ea-private/logs/update.log`.

## What the user sees

- **The doctor**, `updates`: the installed version, the channel, the
  newest release on it, with `N changes behind` when it is newer than the
  installed one (counted in the cache's history at each check), the last
  apply and its result, and any version held back. It warns when a
  version is held back, when the last apply failed or the last run
  stopped on an error (no trust root yet, no network), when a release
  changes the release key, when the timer has not checked in two days, or
  when a channel is set on a tree that still needs the root step. It
  never fails an install.
- **The morning briefing**: one line when the installed version changed
  since the last briefing, with the number of changes since then and the
  first two subjects, `Updated to v0.1.7 (3 changes: <first>; <second>…).`,
  and `Owner step: <step>.` when a release names one (`Updated to v0.1.7
  (from v0.1.5).` when no notes were recorded); or a version was held
  back, `Update v0.1.8 held back; running v0.1.7.`
  `run-agent.sh` appends it after the model's text, from the owner-private
  status (`preprocessor/update_status.py briefing-line`), and records it
  as said only once the post went out.
- **`version`** (or `/version`) in any chat room: the installed tag, its
  commit and the channel, with the newest release on the channel, the
  first three changes of the installed release, its owner steps and the
  link to its notes, and any held-back version. No model call.
  `python3 preprocessor/update_status.py notes` prints every change.

## The relay host

The public relay host runs no EA. Its updater is the same script with the
`relay` verb, run as root by `relay/tacitus-relay-update.timer` (hourly,
randomized) from the relay's own clone:

```
/usr/bin/python3 -I /opt/tacitus-relay/src/scripts/update.py relay
```

It follows `latest` unless `/etc/tacitus-relay/update-channel` names
another channel (one word: `latest`, `stable` or `off`), and keeps its
cache and status in `/var/lib/tacitus-relay/`. Its trust root is
`/var/lib/tacitus-relay/release-key.pub`, outside the clone a release
replaces: the first run copies the clone's `.release-key.pub` there, and
a new key is the operator's to copy in (the same rules as an install's
pin). For a newer signed release on `main` it exports that tag's tree to
a scratch directory and runs **that tree's** `relay/install.sh --tag
<vX.Y.Z>`. Then the clone must be at the verified
commit and `http://127.0.0.1:8471/healthz` must answer; otherwise it runs
the previous release's `install.sh --tag <old>` and holds the version back.

The hook it needs from `relay/install.sh`:

- `--tag vX.Y.Z` installs exactly that tag: the clone at
  `/opt/tacitus-relay/src` ends at the tag's commit, and the script exits
  non-zero when the relay does not answer `/healthz`.
- It installs `relay/tacitus-relay-update.service` and
  `relay/tacitus-relay-update.timer` into `/etc/systemd/system/` and
  enables the timer.

Until `install.sh` takes `--tag`, `update.py relay` finds no `--tag`
option in it, says so and installs nothing.

## Supply chain

What an install trusts, and why a single compromised piece does not get
code onto it:

- **The release key, pinned on the install** ("The trust root"). The
  key that verifies a release is the one the owner's `update.json` pins,
  never a file in a release. A signed release that is malicious cannot
  move the trust root to a key of its own: a new key installs only after
  the owner adds it.
- **A release is on `main`.** The updater fetches `main` with the tags and
  installs a tag only when its commit is in `main`'s history. A leaked
  signing key cannot release a commit that never reached `main`, and
  `main` takes a commit only through a reviewed pull request.
- **One key type.** The pin and the committed key list hold only
  `ssh-ed25519` keys, and git must report an ED25519 signature.
- **The signing job.** The `release` job runs in the `release`
  environment, which only `main` deploys to, behind a required reviewer.
  Its one action is pinned by commit, with the version in a comment. One
  step has `RELEASE_SIGNING_KEY` in its environment: shell, `ssh-keygen`
  and git, a 0600 key file deleted before the step ends, nothing exported.
  The Python steps (the next version, the allowed signers, the verify, the
  notes) run without it.
- **Owner steps from signed history.** The briefing passes each owner
  step to the owner as an instruction, so the steps come only from commit
  messages in the verified range and the signed tag's message. A Release
  body or a pull request body can change after the merge; the updater
  shows a Release's list of changes and never takes a step from it.

What it rests on: the repository settings below. Whoever can change them,
or approve a release deployment, can make a release.

### The repository settings (the maintainer, once)

The workflow cannot make or see these; GitHub creates a missing
environment the first time a job names it, with no protection, and the
job's first notice says only where the key comes from.

1. **The `release` environment.** Settings > Environments > New
   environment, `release`:
   - Deployment branches and tags: *Selected branches and tags*, the
     branch `main` only.
   - Required reviewers: the maintainer, with *Prevent self-review* when
     a second maintainer can approve. Every release then waits in Actions
     for an approval; a run left unapproved is a release that does not
     happen.
   - Environment secrets: `RELEASE_SIGNING_KEY`, the whole private key.
     Then delete the repository secret of that name (Settings > Secrets
     and variables > Actions), so only the environment holds the key.
2. **A tag ruleset.** Settings > Rules > Rulesets > New tag ruleset,
   targets `refs/tags/v*` and `refs/tags/channels/*`, enforcement
   active: restrict creations, restrict updates, restrict deletions,
   block force pushes. Bypass list: the GitHub Actions app only (the
   token the release job pushes with). `channels/stable` is moved by hand
   (`scripts/cut-tag.sh --stable --push`): give that one pattern its own
   ruleset with the repository admin role as its bypass, or move the
   pointer from a workflow.
3. **One review on `main`.** A branch ruleset on `main`: require a pull
   request before merging with one approving review, dismiss stale
   approvals on a new push, require the `ci` check, block force pushes and
   deletions, and no bypass. A sole maintainer needs a second account
   with write access to approve, since GitHub refuses an approval of
   one's own pull request.

An install's owner has nothing to do for the pin; a key change is the
one owner step, as "The trust root" says.

## One-time setup (the maintainer)

1. Make the signing key, on a machine you trust, with no passphrase (the
   workflow cannot type one):

   ```bash
   ssh-keygen -t ed25519 -N '' -C tacitus-release -f tacitus-release
   ```

2. Create the `release` environment and put the private half in it as
   the secret `RELEASE_SIGNING_KEY`, the whole content of
   `tacitus-release` ("Supply chain", the repository settings, step 1).
   Then delete the private file, or keep it offline.
3. Commit the public half as `.release-key.pub` on a branch and merge it:
   `cp tacitus-release.pub .release-key.pub`. The first green push to
   `main` after that is the first signed release.
4. The tag ruleset and the review on `main` ("Supply chain", steps 2
   and 3).
5. Mark the first stable release once it has run for a while:
   `scripts/cut-tag.sh --stable vX.Y.Z --push` on a checkout at that tag.

## Turning it on for an existing install

An install made before the updater has a plain `/opt/ea-system` and no
`update` key, and it keeps deploying from its checkout's hook until the
owner turns updates on:

1. Pull the release that carries the updater (`git pull` in the
   checkout; the hook deploys it).
2. Set the channel: `"update": {"channel": "auto"}` in
   `~/.ea/config/ea-config.json`, or `~/.ea-private/update.json`.
3. Run the root step once: `sudo bash deploy/harden-permissions.sh`
   (Linux) or `sudo bash deploy/macos/install-services.sh` (macOS). It
   moves the tree into the release layout and installs the timer.
4. `python3 /opt/ea-system/scripts/update.py status` shows the result; the
   next timer run installs the channel's newest release.
