# The hosted Tacitus app

This site serves the Tacitus web chat app to a computer that cannot reach
its owner's bot directly: a work laptop, a library computer, a machine
without Tailscale. The owner opens "Add a device" in the chat on a phone
or computer that is already signed in, chooses "A computer", and gets a
short code. On the new computer they open this site, type the code, and
approve the six-digit code it shows on the first device. From then on the
computer talks to the bot through a relay, and every message is sealed
end to end between the two.

The code comes from the public Tacitus repository
(<https://github.com/arpg/tacitus>), and nothing here changes it. This
repository holds only the build, the release key it checks against and
the list of relays.

## What is in this repository

| File | What it is |
|---|---|
| `.github/workflows/deploy.yml` | Builds and publishes the site, every hour and on demand |
| `build.py` | The build: picks the newest signed release, checks it, copies its app (Python standard library and git only) |
| `release-key.pub` | The public half of the key that signs Tacitus releases, one key per line |
| `relays.json` | The relays a computer may use, as a JSON list of addresses |

## What the site serves

- The app's files, exactly as they are in the release: the folder
  `botcore/botcore/chat/webui/` of the release tag.
- `relays.json`, from this repository. A computer that types a code asks
  each relay in this list, in order, for the sealed setup it belongs to.
  A bot whose relay is not listed adds its relay to the address it shows.
- `release.txt`: the release it serves, in three lines: `tag`, the
  release tag; `commit`, the commit the tag names; `files`, the sha256 of
  the app's list of files and their hashes (build.py says how it is made).
  A bot at the same release names the same list in the manifest it signs.

## How a release gets here

Every hour the workflow:

1. Fetches the release tags and the `main` branch of the public
   repository. Nothing from it runs.
2. Takes the newest tag `vX.Y.Z` and checks it the way an install's
   updater does (docs/updates.md in the Tacitus repository, "The trust
   root"): an annotated tag with its own name inside it, signed with SSH,
   by a key in this repository's `release-key.pub` (never a key from the
   release itself), with an ED25519 key; the commit is in the history of
   `main`; and the release's own key list names no key that
   `release-key.pub` lacks. A tag that fails a check is skipped, and the
   next older one is tried.
3. Reads this site's `release.txt`. When it already names that release,
   or a newer one, the run stops there.
4. Otherwise it checks the tag again in a second job, copies the app,
   writes `relays.json` and `release.txt`, and publishes the site with
   GitHub's own Pages actions.

The workflow uses no secret and holds no token that can write to any
repository. Each action it uses is pinned to one commit.

## Who may change this repository

Only the owner of the Tacitus project keeps write access here. No one
else, no team and no bot account.

The reason: whoever can push to this repository decides what code this
site serves, and so what code runs on every computer paired through it.
Changed code could read everything a paired computer shows in its chat
and type as its owner. The chat checks the app it runs against the code
its own bot signed, but it trusts this site at two moments: when a
computer is set up here, and each time the browser fetches the app's
service worker (`sw.js`) from here. So:

- Keep write access to the owner's account alone, with two-factor
  sign-in.
- Keep the branch rule on `main` that blocks force pushes and deletions.
- Never add a deploy key, a bot account or a personal access token with
  write access.
- Publish no other Pages site from this organization. GitHub serves
  every other repository's site at `<owner>.github.io/<name>/`, the same
  address as far as the browser is concerned, and a page there could
  use a paired computer's key.
- Review every change to `build.py`, `deploy.yml`, `release-key.pub` and
  `relays.json` as you would a change to the app.

A computer paired through this site is as safe as this repository's
access list. A phone or computer that opens the chat on its own bot's
address (over Tailscale, or on the bot's machine) never depends on this
site.

## Setting it up

The Tacitus repository has an owner script for this. By hand:

1. Create the public repository `<owner>/<owner>.github.io`.
2. Put the files of the Tacitus repository's `hosted/` folder in it:
   `deploy.yml` as `.github/workflows/deploy.yml`, `build.py` and this
   `README.md` at the top.
3. Copy the Tacitus repository's `.release-key.pub` here as
   `release-key.pub`. Check it against the key the Tacitus project
   publishes before you commit it.
4. Write `relays.json`: a JSON list of relay addresses, each
   `https://host/path` with no slash at the end, for example
   `["https://relay.example.org/relay"]`. `relays.json.example` in the
   Tacitus repository has that form.
5. Settings > Pages > Build and deployment > Source: GitHub Actions.
6. Settings > Rules > Rulesets > New branch ruleset on `main`, active:
   block force pushes, restrict deletions.
7. Actions > deploy > Run workflow. The run's summary shows the release
   it published, and `https://<owner>.github.io/release.txt` names it.

A site under another address (a custom domain) sets the repository
variable `SITE_URL` to its address, so the workflow reads the right
`release.txt`.

## A new release key

When the Tacitus project changes its release key, its releases list the
new key, and this site stops at the newest release the old key covers:
the run warns that the tag changes `.release-key.pub`. Check the new key
against the project's announcement, add its line to `release-key.pub`,
and run the workflow. Remove a key's line when the project retires it.

## Checking what it serves

`release.txt` names the tag and commit. To check the files yourself:

```bash
python3 build.py fetch --src /tmp/tacitus.git
python3 build.py build --src /tmp/tacitus.git --key release-key.pub --tag vX.Y.Z --relays relays.json --out /tmp/site
cat /tmp/site/release.txt
```

The `files` line must match the site's.
