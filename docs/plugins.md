# Plugins

A plugin is one directory with a `plugin.json` at its root. It adds a
capability to the EA (a scheduled digest, a chat trigger, a confirmation
card, a mediated read of another service) without code in EA core. Each
plugin lives in its own git repo, named `<owner>/ea-plugin-<name>` by
convention, so personal features stay out of the core repo.

The design follows OpenClaw's plugin model, without its failure:

- **A manifest at the root.** The manifest declares every capability.
- **Git installs that pin.** `ea-plugin.py install` records the source URL,
  the ref and the commit it resolved to.
- **Capability consent.** A plugin from a source that is not trusted does
  not install until the owner reads its capabilities and passes
  `--accept-capabilities`. An update that changes a capability asks again.
- **Integrity checks.** The lock records the SHA-256 of every file. A
  changed, added or missing file fails `ea-plugin.py verify`, and an
  owner-zone plugin with a failed check does not run.
- **No open marketplace.** OpenClaw's skill marketplace shipped hundreds of
  malicious skills. Here, a plugin comes only from a git URL the owner
  names.

## Two manifest versions

- **v1** (no `manifest_version`): the original format. The plugin's own
  `plugin.json` decides `enabled`, the cron schedule and the chat block.
  Every v1 plugin keeps working as before. Section "v1 reference" has the
  fields.
- **v2** (`"manifest_version": 2`): every capability sits under
  `capabilities`. The owner's lock file decides whether the plugin is
  enabled and which capabilities the owner accepted. Owner processes act
  on the manifest copy in the lock, never on the live `plugin.json`, so an
  edit in the resident's space changes nothing until the owner runs
  `ea-plugin.py update` and accepts again.

## Two trust zones

| Zone | Where it lives | Whose code runs | What it may declare |
|---|---|---|---|
| `agent` (default) | `<EA_DIR>/plugins/<name>`, the resident's space | the resident (`ea-bot`) | schedules, chat, skills, tool grants and denials, cards that run as the resident, mail rules, network endpoints, secrets, a Claude Code plugin |
| `owner` | `<private_dir>/plugins/<name>`, owner only (`~/.ea-private/plugins/`) | the owner, with the owner's tokens | everything above, plus owner hooks (mediated verbs, auth sources, owner cron jobs) and cards that run as the owner |

An owner process loads owner-zone code only from the private directory,
after the `owner_trust` checks (a real directory owned by the owner, not
writable by group or others) and a clean integrity check. It never loads
code from a path the resident can write.

In the agent zone the integrity check gates what the owner's processes
grant: a plugin whose files no longer match the lock gets no tool grant and
no chat skill from the bot, and `run-plugin.sh` refuses its scheduled run
(`plugin_registry.py verify`, exit 3). Each failure posts to the health
channel once per problem set (`plugin-integrity-alerts.json` in the private
directory). The check does not stop the resident from running that code by
other means; it stops an edit from riding the owner's grants and crons. A v1
plugin that is not in the lock has no hashes to check: adopt it
(`ea-plugin.py adopt <name> --accept-capabilities`) to bring it under the
check. The daily persistence digest (`scripts/persistence-digest.py`)
reports a change to any plugin file, locked or not.

## The lock file

`plugins.lock.json` in the owner-private directory (mode 0600). One entry
per installed plugin:

| Key | What it holds |
|---|---|
| `zone`, `enabled` | where the plugin lives, and whether it acts |
| `source` | `url`, `ref` (asked for) and `commit` (resolved) |
| `manifest` | the normalized manifest, as installed |
| `capability_digest` | SHA-256 of the capability summary |
| `accepted` | the digest the owner accepted, when, and how (`accepted` or `trusted-source`) |
| `files` | `{relative path: sha256}` of every file the check covers |

The check leaves out `.git`, `__pycache__`, `*.pyc`, `*.log`,
`.env.secrets` and the plugin's state directories (`state_dirs`, default
`["state"]`). A symlink is a problem.

The resident cannot read the lock. A resident process therefore sees no
v2 plugin capability that needs the lock; the owner's processes (the bot,
the mediator, the crons, `run-plugin.sh`) act on it.

A directory whose name is in the lock is read only through the lock,
whatever its live `plugin.json` says: the registry, `run-plugin.sh` and
`scripts/install/setup.sh` treat a locked name as locked even when the live file now
claims to be a v1 manifest, so a resident that rewrites a disabled
plugin's manifest gets no skill, tool grant or cron line back. The lock
entry's `manifest` also carries the `context` grants a scheduled run may
use (the ledger shim, named `graph_actions` verbs, `fetch_url`, the
calendar and Drive reads); `run-plugin.sh` builds a locked plugin's allow
list from that entry and its accepted `capabilities.tools`, never from the
live file. `ea-plugin.py adopt <name> --accept-capabilities` refreshes an
entry in place (new file hashes, the context block) for a plugin
installed before the block was part of the lock.

## Install policy

`trusted_sources` and `blocked_sources` are fnmatch patterns over the
source URL. The owner-private `plugins-policy.json` holds them, and it is
the only source: `ea-config.json` is the resident's file, so a trusted
source named there would let the resident install without consent. With
no policy file every install needs `--accept-capabilities`. An adopted
agent-zone plugin always needs it, since its origin URL is read from a
`.git/config` in the resident's space.

- A source on `blocked_sources` never installs, with or without consent.
- A source on `trusted_sources` installs without `--accept-capabilities`,
  unless the plugin runs code as the owner (owner zone, owner hooks, or an
  owner card executor). As in OpenClaw, trusting a source never grants
  owner tools.
- Any other source needs `--accept-capabilities`.

```json
{"trusted_sources": ["https://github.com/<owner>/ea-plugin-*"],
 "blocked_sources": []}
```

## ea-plugin.py

Run as the owner. Nothing here pushes. Every git step runs in the
owner-private staging directory, never in the resident's space, where a
repository's config or hooks would run as the owner.

| Command | What it does |
|---|---|
| `install git:<url>[@<ref>]` | clone, check the manifest, print the capabilities, ask for consent, place the files, record the lock entry. `--disabled` installs it switched off. |
| `adopt <name> [--zone owner]` | record a plugin already in place (for example a v1 plugin cloned by hand) without fetching it again. Reads its `.git` files as text; runs no git there. |
| `verify [<name> ...]` | compare installed files with the lock. Exit 1 on a problem. |
| `update <name> [--ref <ref>]` | fetch the release, ask for consent again when a capability changed, replace the files, keep the state directories. |
| `enable <name>` / `disable <name>` | flip `enabled`. Enable asks for consent when the accepted digest no longer matches. |
| `uninstall <name> --yes` | remove the files and the lock entry. An adopted v1 plugin keeps its directory. |
| `list [--json]` | every locked plugin, and any unmanaged plugin directory |
| `show <name>` | the capabilities, as the lock holds them |
| `cron-lines` | crontab lines for the owner plugins' cron jobs |
| `run-owner <name> <label>` | run one owner cron job after the checks (cron calls this) |

Exit status: 0 done, 1 problems found, 2 usage or a bad manifest, 3
consent needed, 4 blocked by the policy.

An agent-zone install copies the files, without `.git`, into the
resident's plugins directory as the resident. An owner-zone install moves
the checkout, with `.git`, into the private directory at mode 0700.

After an install or an update, restart the chat bot and the mail mediator
to load cards, chat triggers, verbs and tool denials; re-render the
resident's network policy when the plugin declares a local endpoint; add
the cron lines `run-plugin.sh --check-schedules` and `ea-plugin.py
cron-lines` name. `scripts/install/setup.sh` writes one `run-plugin.sh` line per schedule
of each plugin the lock enables, as it does for a v1 plugin's `schedule`
and `extra_schedules`.

## Manifest v2

```json
{
  "manifest_version": 2,
  "name": "example",
  "version": "0.1.0",
  "description": "What it does, in one line.",
  "zone": "agent",
  "capabilities": {
    "schedules": [{"label": "daily", "cron": "0 9 * * 1-5", "llm": "remote-first",
                   "model": "sonnet", "timeout_seconds": 600, "skill": "skill.md"}],
    "chat": {"skill": "chat_skill.md", "keywords": ["<phrase>"], "patterns": []},
    "skills": ["skill.md"],
    "tools": ["Bash(python3 {plugin_dir}/*.py *)"],
    "denied_tools": ["Bash(python3 *tool.py send *)"],
    "cards": [],
    "mail_rules": {"closure_confirm_senders": [], "closure_exclude_senders": [],
                   "keep_from_anthropic_domains": []},
    "network": {"domains": ["api.example.org"], "local": []},
    "secrets": ["EXAMPLE_TOKEN"],
    "owner_hooks": {"verbs": [], "auth_sources": [], "cron": []},
    "claude_plugin": ""
  },
  "state_dirs": ["state"],
  "scopes": [],
  "output": {"channel_key": "personal-example", "format_anchors": [":sparkles:"]}
}
```

`tests/fixtures/plugins/` holds two complete made-up examples:
`example-notes` (agent zone, every agent capability) and
`example-chat-reader` (owner zone, every owner hook).

### Capability reference

**schedules.** One entry per cron line. `label` is unique; `args` are the
extra arguments `run-plugin.sh <name> -- <args>` passes to `collect.py`,
and they pick the schedule. `llm` says who writes the post:

| `llm` | The post |
|---|---|
| `claude` | Claude writes it from the skill and the collected JSON (the v1 path) |
| `remote-first` | the remote model writes it, with no tools; Claude writes it when the remote model is off, fails or writes a post that fails the checks |
| `remote` | the remote model writes it; a failure posts a warning |
| `none` | no model: `collect.py` writes the post itself as the string `post` in its JSON |

The checks are the same for every writer: no meta-commentary, and the
first line opens with one of `output.format_anchors`.

**chat.** The interactive trigger (the older name `slack` is accepted).
When a message matches a keyword or a pattern, the bot loads the skill
into its prompt. That skill runs with the bot's tool allowlist, so it is
as privileged as a core skill.

**skills.** Skill files the plugin ships, listed for consent.

**tools.** Agent tool grants. Each must name `{plugin_dir}` and nothing
else, so a grant reaches only the plugin's own files. A chat plugin with
no `tools` gets `Bash(python3 {plugin_dir}/*.py *)`. A grant is a
template: every lane expands it into one rule per file the lock hashed
(`plugin_registry.expand_tool_grants`), where `*` stays inside one path
segment and `**` spans segments. No rule names a file under a state dir,
because the lanes may write there.

**denied_tools.** Tool denials that join every lane's deny list (the bot,
`run-agent.sh`, `run-plugin.sh`). Use them for a plugin's own gated verbs,
so no model reaches them except through a card.

**cards.** Confirmation cards. A lane or a cron job stages items (a JSON
object, or an array of them) in the card's `pending_file`; the bot claims
the file with the same protocol as the core cards, posts one card per item
in the owner's DM, and on a click runs the executor.

```json
{
  "id": "publish-note",
  "pending_file": "pending-example-notes.json",
  "title": ":lock: *Publish note* {title}",
  "lines": ["_{summary}_"],
  "approve_label": ":memo: Publish", "approve_word": "publish",
  "cancel_label": "Skip", "cancel_word": "skip",
  "button_noun": "The Publish button",
  "busy_status": ":hourglass_flowing_sand: Publishing...",
  "required": ["title"],
  "missing_text": ":warning: The note card for {summary} needs a title.",
  "key_fields": ["title"],
  "variants": [{"when": {"draft": true}, "title": ":lock: *Publish draft* {title}"}],
  "approve": {"command": ["python3", "{plugin_dir}/card_exec.py", "approve"],
              "user": "resident", "timeout_seconds": 60},
  "cancel": {"command": ["python3", "{plugin_dir}/card_exec.py", "cancel"],
             "user": "resident"}
}
```

- Text keys are templates over the item's fields (`{title}`). Attribute
  and index access is refused; a missing field reads as empty.
- The first `variants` entry whose `when` matches the item lays its keys
  over the card.
- An item that lacks a `required` field gets `missing_text` in the thread
  and no card.
- On the card each item value is inert text (`card_text.inert`): one
  line, shown as written, no markup. A value longer than 300 characters
  is cut with a marker, and a card whose templates show a cut value has
  no approve button, since the executor gets the whole item.
- `pending_file` matches `pending-<name>.json`. A plugin cannot take a
  core staged file.

The executor contract (`preprocessor/plugin_cards.py`):

- The item arrives as JSON on stdin and in `EA_CARD_ITEM`, with
  `EA_PLUGIN_NAME`, `EA_PLUGIN_DIR`, `EA_CARD_ID` and `EA_CARD_ACTION`.
- On approve, the bot mints a one-time token for that card and that exact
  item, valid 15 minutes, and passes it in `EA_CARD_TOKEN` (and wherever
  the command names `{token}`). The executor calls
  `plugin_cards.consume_token(token, plugin, card_id, item)` before the
  gated step.
- The last stdout line that is a JSON object with an `ok` key is the
  result: `{"ok": true, "status": "<card line>", "message": "<thread line>"}`.

Where the token lives decides what the check is worth. On a mediated
install the bot mints it into the owner-private directory, which the
resident cannot write, and a resident executor's `consume_token()` sends
`plugin_cards consume-token` through the mail mediator, which takes the
token as the owner; a token file the resident writes in its own space is
never read. On an install with no mediator the token file is in the
resident's state directory, where the executor reads it itself.

A resident executor still runs as the resident, with the resident's files
and credentials, so an injected model could call the same client the
executor calls, without the executor. The token check keeps the executor
from running without a click; it does not make the action impossible for
the resident. An action that must never run without a click belongs in an
owner-zone plugin, whose executor runs as the owner (`"user": "owner"`).

**mail_rules.** `closure_confirm_senders` are sender substrings whose mail
can close a ledger row (as `CONFIRMATION_SENDER_SUBSTRINGS` in
`closure_scrubber.py`); `closure_exclude_senders` never count, even when a
core substring matches; `keep_from_anthropic_domains` join the remote
model's keep list (`remote_llm.py`). A substring shorter than six
characters, or with no `@` or `.`, is refused as too broad.

**network.** `domains` are the internet hosts the plugin reaches, listed
for consent (the resident's internet access stays open). `local` entries
(`daddr`, `proto`, `ports`, `reason`) join the resident's network
allowlist as `plugin:<name>` (`resident_netpolicy.py`). A root render
takes them from a facts file the owner wrote with `resident_netpolicy.py
facts`.

**secrets.** The secret names the plugin reads, listed for consent. The
values stay in the plugin's `.env.secrets` (agent zone) or the owner's
private `secrets.env`.

**owner_hooks** (owner zone only):

- `verbs`: commands the resident calls through the mail mediator. The
  resident runs `plugin_verbs.py <plugin> <verb> [args]`, or the `alias`
  the verb declares on a core tool (`graph_actions.py <verb>` when
  `"alias": {"tool": "graph_actions", "verb": "<verb>"}`); a core verb
  always wins over an alias. The request travels as tool `plugin:<name>`.
  The mediator runs it only when the lock has the plugin enabled, its
  capabilities accepted, its files unchanged and the verb `safe`. A
  `gated` verb never runs from the resident.
- `auth_sources`: `{"kind": "graph", "module": "<file>.py", "function":
  "<name>"}`. Each function takes no arguments and returns a token or
  None. `graph_client.authenticate` asks each one for a token after the
  MSAL cache and before any interactive sign-in. A token whose audience
  is Outlook REST routes requests to that API; a token with an `exp`
  claim is kept in the process until a minute before it expires.
- `cron`: `{"label", "cron", "command"}` jobs that run as the owner.
  `ea-plugin.py cron-lines` prints the crontab lines; each calls
  `ea-plugin.py run-owner <name> <label>`, which checks the plugin first.

**claude_plugin.** A directory (relative to the plugin, `.` for the plugin
itself) that is a Claude Code plugin root, with `.claude-plugin/plugin.json`
and its `skills/`, `agents/`, `hooks/` and MCP servers.
`plugin_registry.claude_plugin_dirs()` lists them, and
`claude_cli.plugin_dir_flags()` turns them into `--plugin-dir` flags that
the chat lanes pass (topics, the DM, rooms; not the remote arm's bare
harness). `--plugin-dir` works in `--bare` mode too. Its hooks run in
agent sessions, so they are part of what the owner accepts.

Commands in cards, verbs and cron jobs are lists of strings. Each must run
code inside `{plugin_dir}`. The placeholders are `{plugin_dir}`,
`{python}` and, in a card's approve command only, `{token}`.

An owner verb or cron job runs from its plugin directory with
`EA_PLUGIN_NAME`, `EA_PLUGIN_DIR`, `EA_CORE_DIR` (the core checkout) and
the core's `preprocessor` directory first on `PYTHONPATH`
(`plugin_registry.owner_command_env`), so its code imports `owner_trust`,
`log_paths` and the other core modules it builds on. Core code that needs
a verb's output calls `plugin_verbs.call_alias(tool, verb, args)`, which
returns `(returncode, stdout, stderr)`, or None when no enabled plugin
declares the alias.

## v1 reference

```json
{
  "name": "<short-name>",
  "version": "1.0",
  "enabled": true,
  "schedule": "0 3 * * *",
  "extra_schedules": [{"label": "nightly", "cron": "0 2 * * *", "args": "--enrich"}],
  "model": "sonnet",
  "timeout_seconds": 1800,
  "scopes": [],
  "chat": {"skill": "skill.md", "keywords": ["<phrase>"], "patterns": []},
  "context": {"ledger_read": "own", "ledger_write": "own", "graph_actions": [],
              "google_calendar": false, "google_drive": false, "fetch_url": false,
              "ea_briefing": false},
  "output": {"channel_key": "personal-<short-name>", "format_anchors": [":sparkles:"]}
}
```

- `schedule`, `extra_schedules`, `model`, `timeout_seconds`: the cron
  passes, as in v2 `schedules` with `llm: claude`.
- `scopes`: the preference scopes whose user rules `run-plugin.sh` appends
  to the skill. A `<!-- scopes: ... -->` header in the skill works too.
- `context`: grants for the scheduled run. `ledger_read` (`own`, `all` or
  false) and `ledger_write` (`own` or false) go through the
  `ea_plugins.ledger` helper, which sets `source = 'plugin:<name>'` on every
  write. `graph_actions` lists the subcommands the run may call; each
  becomes one allowlist entry. A plugin never imports `graph_client`: under
  mail mediation the resident has no token. `from ea_plugins import mail`
  gives mediated reads.
- `output.channel_key`: the chat route (on Zulip, `chat.zulip.notify` in
  `ea-config.json` maps it to a stream and topic).
  `output.format_anchors`: the shortcodes a post may open with.
- `chat` (or `slack`): the interactive trigger, as in v2.

v2 keeps `scopes` and `output` at the top level, with the same meaning.

## run-plugin.sh

`run-plugin.sh <name> [-- <collect args>]` runs one scheduled pass:

1. Read the manifest (as the resident; it is the resident's file). A
   plugin the owner's lock names runs from the manifest in the lock
   instead (`plugin_registry.py locked`, `manifest`): the lock decides
   `enabled`, and the schedule whose `args` match gives the model, the
   timeout, the skill and `llm`. A locked plugin that is disabled is
   skipped; one whose files no longer match the lock is refused (exit 1)
   and reported to the health channel.
2. Run `collect.py` as the resident, with `EA_PLUGIN_NAME` and
   `EA_PLUGIN_DIR`, into `state/last-collect.json`. `"should_post": false`
   in its JSON ends the run quietly.
3. Write the post: `plugin_llm.py` for `none`, `remote` and `remote-first`;
   otherwise Claude, with the skill, the collected JSON and the allowlist
   the `context` grants and, for a locked plugin, its accepted
   `capabilities.tools` build (a v1 plugin gets python3 on anything under
   its directory). Tool denials from `scripts/disallowed-tools.txt` and
   from enabled plugins apply.
4. Check the post (no meta-commentary, a format anchor first), post it to
   `output.channel_key`, then commit `state/state.pending.json`.

Logs go to `~/.ea-private/logs/plugins/<name>/YYYY-MM-DD.log`.
`run-plugin.sh --check-schedules` compares every manifest's schedules with
the crontab and reports drift both ways.

## Shared ledger

Plugin rows live in the main ledger with `source = 'plugin:<name>'`. EA
core filters them out of its briefings; a plugin sees its own rows. The
lifecycle (snooze, follow-up, deadline-watchdog, closing a row when its
email is archived) works for plugin rows unchanged, as long as the plugin
stores `source_message_id`.

## Moving a core feature into a plugin

1. Create the plugin repo ([arpg/tacitus-plugin-template](https://github.com/arpg/tacitus-plugin-template)
   is the skeleton, MIT) and write
   a v2 manifest that declares what the feature does.
2. Move the code: the preprocessor part to `collect.py`, the skill to the
   plugin, the keywords to `chat`, a hard-coded card to `cards`, sender
   rules to `mail_rules`, gated verbs to `denied_tools`, owner-side code
   to an owner-zone plugin with `owner_hooks`.
3. Install it with `ea-plugin.py install`, check the plugin works, then
   delete the core copy. Grep core for the feature's terms; only generic
   plugin plumbing may remain.
4. Retag the feature's ledger rows (`UPDATE ledger SET source =
   'plugin:<name>' WHERE ...`) and swap the cron line to `run-plugin.sh`.

## Code map

| File | Role |
|---|---|
| `preprocessor/plugin_manifest.py` | read, check and normalize a manifest; the capability summary and digest |
| `preprocessor/plugin_registry.py` | the lock, zones, integrity, and the capability reads the core makes |
| `preprocessor/plugin_install.py`, `scripts/ea-plugin.py` | install, adopt, verify, update, enable, uninstall |
| `preprocessor/plugin_cards.py` | card rendering, tokens, executors |
| `preprocessor/plugin_verbs.py` | owner verbs as the resident calls them; core tool aliases |
| `preprocessor/plugin_llm.py` | schedule posts that Claude does not write |
| `ea-chat-bot.py` | plugin cards in the claim protocol (`PLUGIN_ACTION_FILES`), the card handlers, plugin tool grants and denials |
| `preprocessor/mail_mediation.py`, `mail_mediator.py` | `plugin:<name>` tools, classified and run from the lock |
