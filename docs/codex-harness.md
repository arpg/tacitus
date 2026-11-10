# Running a lane on Codex

The EA's agent lanes run on Claude Code (`claude -p`, built by
`preprocessor/claude_cli.py`). The same skill can be measured on OpenAI's
Codex CLI (`codex exec`) through `preprocessor/codex_cli.py`, the one
builder for a non-interactive Codex run. Today that covers the briefing
lane of the evals and the release suite; it is a measurement harness, not
a production lane.

## What differs

- **Instructions.** Codex loads no CLAUDE.md. The prompt carries the
  core operating instructions (`prompt_logic.build_claude_md` with
  nothing discovered) ahead of the same framing the Claude run gets
  (`tests/evals/provider.py _claude_prompt`).
- **Tools.** There is no allow list. Codex runs shell commands inside its
  own sandbox: `read-only` reads anywhere and writes nowhere;
  `workspace-write` writes only under the working root (`-C`), the
  `--add-dir` directories and the temp dir (see "Temp files" below). The
  eval sets the root to the run's throwaway EA_DIR, so the test ledger and
  a staged event land there and the code tree stays read-only. Network is
  off inside the sandbox; a live turn's tools reach it through the
  mediator (see "The network" below).
- **Output.** The final agent message (`-o`) is the result; the JSONL
  event stream gives the commands it ran (`command_execution` items,
  reported as Bash tool calls), the files it changed, and the token usage.
- **Login and model.** Codex uses its own login (`codex login`) and the
  model in `~/.codex/config.toml`; `--model` names a Codex model, and
  `default` keeps the local one. Runs spend that account's quota, not the
  Claude subscription. `EA_CODEX_BIN` names another binary (a wrapper or
  a pinned version); the doctor's `EA_CODEX_CLI` names another probe
  script, which only its tests use.

## Commands

```
python3 preprocessor/codex_cli.py probe        # exit 0 when codex answers a trivial prompt
python3 preprocessor/codex_cli.py version
python3 scripts/ea-doctor.py --probe-codex     # the same, as a doctor line
python3 tests/evals/run_evals.py --mode full --harness codex --fixture regression-009
python3 tests/release/run_release.py --lane briefing --harness codex --tag codex-check
```

A release run on Codex is scored by the same assertions and judge as a
Claude run and compared only with earlier Codex runs, never with a
Claude baseline.

The probe starts in a new folder under `~/.ea-codex-probe` (mode 700, in
the passwd home of the user who runs it, not `$HOME`), never under /tmp.
Codex takes the folder of a `.git` above its working directory for the
project root and loads the skills in the `.agents` beside it, and any
user can make both in /tmp. The probe is refused when another user can
write a folder above its own.

## The ghost arm

`preprocessor/codex_ghost.py` runs Codex beside the production Claude
lanes for a window of days, with nothing posted and nothing written, and
grades it against Claude. Claude stays the workhorse throughout.

- **Open a window:** `python3 preprocessor/codex_ghost.py start --days 7
  [--kinds briefing,chat] [--share 0.5]`. It writes `ghost.codex` in the
  owner-private remote-llm.json. `stop` removes it; `status` and `report`
  read it. `restart` (same options) first moves the last window's turns,
  grades, recorded writes, verdicts and queued jobs to
  `codex-ghost/archive/<from>_<until>/` and forgets the ghost Codex
  threads, so the new window is measured from zero.
- **What is replayed.** `briefing`: after the morning briefing or the
  afternoon update, run-agent.sh hands the exact prompt it gave Claude to
  `enqueue-lane`, with Claude's report as the reference. `chat`: after a
  turn Claude answered in the EA channel, the DM or a room, the bot hands
  the context block and the user's message to `enqueue` (a share of the
  turns, `--share`). Each job runs detached; the production turn never
  waits for it. A chat turn whose message names a deck, a link, a Google
  document, GitHub, Overleaf or a logo is not replayed (`DEFAULT_SKIP`,
  read against the user's words, not a room's preamble): a ghost's
  sandbox has no network and only its page reads go through the mediator
  (see "The network" below). A replay whose commands still hit the
  missing network is recorded as skipped.
- **How a replay runs.** As the resident, never as the owner: Codex's
  sandbox confines writes, not reads, and a model shell started as the
  owner could read the owner's secrets. The working root is a scratch
  EA_DIR under `~ea-bot/.ea/state/codex-ghost/` (inside a root the
  mediator already reads inputs from and delivers outputs to, so a body
  file or a downloaded attachment under it passes the same checks a
  production turn's does) holding copies of the ledger, the project
  store and the briefing, and links to the config, the docs and the
  mediator's queue. The remote writer, the style store and the solver's
  reads (`propose-meeting`, `validate-draft`, `check-attendees`,
  `reschedule-brief`) answer a ghost as they do a production turn, and a
  `--help` runs as the verb's usage alone. The run id starts with
  `cron-ghost-codex-`, so the mail gate treats it as a cron lane, and the
  mediator answers every other verb in place of running it
  (`ghost_answer`): a draft verb in the real verb's JSON with a synthetic
  draft id (`ghostdraft-...`), so the turn goes on as in production, and
  an archive or a snooze with `{"status": "recorded"}`. Each call, with a
  draft's body, is written to `recorded-writes.jsonl` and never executed.
  A read of a ghost draft id answers from that record. Network is off.
- **One thread per replay.** A chat replay runs the fresh prompt the bot
  built for the turn (it carries the thread's history) on a new Codex
  thread and a new scratch, both gone after the turn. A resumed ghost
  thread carried earlier turns' stub answers into later turns.
- **Records and grades.** `turns.jsonl` holds Claude's output, Codex's
  output, its commands, usage and timing. `grade` runs the ghost week's
  judge (Sonnet as ea-bot, or `--judge remote`): a briefing is `same`,
  `minor` or `major` with the items missing, extra or wrong; a chat turn
  is `send`, `edit`, `wrong` or `none` with problem kinds, and Claude's
  reply gets the same grade. The chat judge counts a recorded draft call
  with a body as the draft; the briefing judge gets the user's zone and
  reads a UTC time and its local twin as one fact. `report` gives the
  verdict per kind against `PASS` (GO, NO-GO, NOT ENOUGH DATA), the wrong
  and major lists and the judge's enhancement lines. Chat is measured
  against Claude on the same turns: Codex's send share may trail
  Claude's, and its wrong share exceed Claude's, by at most 10 points.
  `final` writes the verdict files that `gate` reads.
- **Quota.** `limits` reads the account's 5-hour and weekly windows
  through `codex app-server`; `enqueue` skips, and records the skip, once
  a window passes `QUOTA_STOP`.
- **Setup.** `deploy/enable-codex-ghost.sh` as the owner, once: it copies
  the owner's Codex binary into ea-bot's space, writes ea-bot's
  config.toml, signs ea-bot in with a device code and probes.

## Production

The owner moves every task to Codex, and back, with one switch: "Hosted
model" at the top of the web chat's Models page, or `model_routing.py
set-hosted codex|claude` (the routing table's `hosted_default`,
docs/model-routing.md, "The hosted model: one switch"). A task can be
set apart from it (an override). `chat` and `scheduling` then run their
lanes on the `codex` tier, `briefing`, `audit` and `digests` run their
Claude lane on Codex as below, with no gate (`model_routing.py
harness`), and `tagging`, `thread_state` and `review` make their
tool-less call on Codex ("Tool-less calls" below). `mail` and `drafting`
have no hosted model of their own and stay on Claude.

The code that runs a turn or a call on Codex is the harness: the bot's
`_HARNESS_TURNS` (`codex_turn.run_turn`), `hosted_text.CALLS`
(`hosted_text.codex`), the lane runner in `run-agent.sh`
(`codex_ghost.py run-lane`) and this page's modules. Everything else
asks `model_routing` by class (`is_hosted`, `is_base`, `for_model`) and
finds Codex by its entry in `ea_config.HOSTED_MODELS`, so a second model
on the Codex CLI is one more entry there.

`EA_HARNESS=codex` in the environment of `run-agent.sh` runs a briefing
lane on Codex for real: `codex_ghost.py run-lane --live` as the resident
against the real EA_DIR, from `~/.ea/state/codex-lane` (`LANE_CWD`, made
as the resident, mode 750), writes confined to its state and docs, the
same prompt and the same output handling. The Claude lane starts in the
resident's home; the Codex lane does not, because Codex makes its working
directory writable (see "Temp files"). The prompt names every file by
its full path, and a line after it says where the Codex run starts. It runs only when `codex_ghost.py
gate briefing` finds a GO no older than 14 days (`EA_HARNESS_FORCE=1`
overrides); otherwise the Claude run goes ahead and the log says why.

**Chat turns** (`preprocessor/codex_turn.py`): the arm `codex` runs a
chat turn on Codex as the resident. The bot builds the turn's fresh
prompt as it does for Claude, and `run_turn` sends it, behind what a
Claude run would load by itself, to a new Codex thread (`codex exec`;
see "No resume" below), with writes confined to the folders a Claude
turn writes and the run id the bot minted, so drafts and staged events
surface as after a Claude turn. Every chat lane gets the arm with the
switch at Codex, a topic with `use codex`, one message with `@codex`, a
lane with `tiers.lanes` in ea-config.json while no switch is set; the
footer says "EA · Codex". `@claude` or `use claude` sends a turn or a
topic back to Claude whatever the switch says. A harness error or an empty answer hands the turn to
Claude. The same function with `live=False` is the ghost's chat path:
the production path with posting off (a scratch EA_DIR per thread, the
real path replaced in the prompt, the mediator recording every write,
the staged files read back for the judge). The email-author subagent
has no Codex twin yet: a turn that needs it uses `email_writer.py`
(the remote writer) or writes the body itself.

**Images in mail.** A turn reads an image on a message (a picture the
body shows inline included) with `mail_actions.py describe-attachment`:
the mediator fetches it and the owner's local image model reads it, so
the turn needs no file and no network. When no model reads images, the
same verb with `--output <EA_DIR>/state/<name>` saves the picture there
(the mediator delivers it as the resident; outside sealed mail only), and
Codex's `view_image` tool opens it: the sandbox confines writes, not
reads. `view_image` is on in chat turns (run_turn passes no `--disable`;
it is off only in the tool-less calls, hosted_text.CODEX_OFF), and the
Codex suffix names both ways.

**What Claude Code loads by itself.** A Claude run loads the instruction
files on its path (CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md from the
root down, and the resident's ~/.claude/CLAUDE.md) and the first 200
lines of its project's MEMORY.md, so the bot's prompt leaves out what it
loads. Codex loads none of them. `codex_turn.context_block` puts the same
set ahead of each Codex prompt, for the directory the Claude run would
have used (`claude_cwd`: the deployed tree, or the lane directory of the
scheduling and slim reply prompts, which loads nothing). Files are read
as the resident (resident_io). The memory is read-only to Codex, as
`resident_write_paths.STEERING_RULES` make it to every Claude lane; a
standing rule of the user's goes to `preferences.py add`.

**What a live turn may write.** A Claude turn's Write and Edit tools may
change only the `resident_write_paths` paths, and its allowlisted scripts
write with the resident's own permissions. Codex's sandbox confines every
write, the scripts' included, so a live turn's writable folders are those
paths plus the scripts' folders (`SCRIPT_WRITE_DIRS`: logs and uploads)
and its temp dir. It runs in `~/.ea/state/codex-live` (mode 750): Codex
makes its working directory writable and cannot start in the read-only
deployed tree.

**Temp files.** By default Codex's workspace-write sandbox also makes
/tmp and `$TMPDIR` writable. In every folder a run may write it mounts
`.git`, `.agents`, `.codex` and `.aws` read-only, and it makes an empty
folder for each one that is missing and removes it when the command
ends. A command that does not end cleanly (its process group killed)
leaves those folders. An empty `.git` in /tmp makes Claude Code and Codex
take /tmp for a project root for every run below it, whoever owns the
run: the eval guard (`tests/evals/provider.py scratch_parent`) refuses a
scratch dir under it, and Codex loads the skills in the `.agents` beside
it. So no Codex run as the resident writes /tmp: a live turn, a ghost
replay and a live lane get `~/.ea/state/codex-tmp` (`codex_ghost.CODEX_TMP`,
made as the resident, mode 750, entries kept for a day after their last
change) as their temp dir (`codex_cli.temp_flags`: /tmp and `$TMPDIR` are
no roots, the folder is one, and the commands get it as TMPDIR, TMP and
TEMP). It sits in the state, which the mediator accepts files from, as
it did /tmp. An eval run on Codex gets a `tmp` folder in its own
throwaway EA_DIR. A working directory is a writable root too, with the
same guard folders, so no Codex run as the resident starts in the
resident's home: a `.git` left there would sit above every Claude lane
folder (`~/.local/state/ea-lanes/<lane>`), and the whole home would be
writable to the run.

The config folder is not a root (`NO_WRITE_DIRS`): a folder root would
cover `ea-config.json`, `github.json` and `CLAUDE.install.md`, which
steer every later run, and `config/autonomy-tiers.json` in the Claude
write paths does not open it. The two verbs that write there from a
turn, `preferences.py add|calibrate|retire` and `onboarding.py answer`,
meet a `PermissionError` in the sandbox and stage the write instead
(`~/.ea/state/preferences-staged.jsonl`, the argv and the run id),
answering `{"status": "staged"}`. After the turn the bot runs
`preferences.py apply-staged` as the resident, outside the sandbox, with
the user's message of that turn on stdin: a rule or an answer is applied
only when the words it quotes (`--quote`, `--text`) are in that message,
so a rule lifted from a mail body is refused and kept in
`preferences-staged-refused.jsonl` with the reason; a calibration or a
retirement carries no quote and is applied as it is. The turn's record
carries the outcome (`preferences`). A tier promotion is a verb too,
`autonomy_tiers.py promote --task <task> --tier <tier> [--contact <addr>]
--quote "<words>"` (CLAUDE.md rule 12), staged and applied the same way.

**The network.** The sandbox has none, and a live turn needs none of its
own. Each tool that reaches the web hands its call to the mediator
(`ea-mail-mediator.service`, step 7 of setup), which runs it outside the
sandbox and answers with the tool's own output and exit status:

| The turn runs | Where it runs | As |
|---|---|---|
| mail, calendar, `drive_client.py`, `scheduling_engine.py`, `email_writer.py` | the mediator | the owner, who holds the tokens |
| `web_browser.py`, `poll_filler.py` | the mediator's browser worker | the resident |
| `fetch_url.py`, `booking_link.py`, the `site_tools.py`, `overleaf_tools.py` and `doc_review.py` git steps | the mediator's resident worker | the resident |
| a plugin's own script, through `plugin_run.py <script> <args>` | the mediator's resident worker | the resident |

The last two rows are `mail_mediation.RESIDENT_NET` and `plugin_run.py`.
They hold no owner credential, so the mediator starts them as the
resident (`sudo -n -u ea-bot env -i ...`) with an environment it builds
itself, in the turn's working directory: the child has the access a
Claude turn's command has and the same egress policy
(`resident_netpolicy.py` matches the user, not the process). Only a
command inside the sandbox forwards (Codex sets
`CODEX_SANDBOX_NETWORK_DISABLED=1` there); a Claude turn, the owner's
shell and the mediator's own child run the tool in place. What holds:

- The gated verbs stay gated. `site_tools.py publish` and
  `overleaf_tools.py push` are not in the table and take the owner's
  one-time token as before. A forwarded `site_tools.py commit` or `revert`
  stages `pending-publish.json` itself and answers `publish_staged`, since
  the turn cannot stage a card for a commit made outside it.
- A plugin script runs only when a Claude turn may run the same command
  line: it matches a plugin tool grant (an enabled plugin whose files
  match the lock) and no denial of the plugins or the core deny lists
  (`plugin_registry.script_call_error`). The sandbox cannot change a
  plugin's code: of a plugin's folder only `state/` is a write root, and
  no grant names a file there.
- Four resident calls run at once; a fifth answers 3 with "busy; no action
  started". A core call is stopped after 300 s and a plugin script after
  600 s, with exit 3 and "its state is unknown", so the turn checks before
  it tries again. The audit log has one line per call.
- A ghost replay forwards a page read only. Every other resident call is
  recorded for the judge and not run.

A turn's own commands (`curl`, `git`, `pip`, a script it wrote) still
have no network, and the Codex suffix says so. Codex's own `web_search`
tool is on in a live turn.

**With no mediator** (an install that skipped mail mediation) nothing
forwards, and the earlier arrangement stands: the bot fetches the links
of the message before the turn (`pages_note`), refreshes the site clones
(`site_tools.py prefetch`) and the doc-review copy, the sandboxed status
and preview read those cached refs, and `commit`, `pull` and `revert`
stage for a replay after the turn, as the resident, with the normal
conflict, content and safe-git checks; a successful commit stages
`pending-publish.json`. Settings then shows the limit under the chat
task (`model_routing.harness_limit`). On a mediated host the refresh
before the turn still runs, and the note is absent.

**No resume.** `codex exec resume` drops the writable roots: the resumed
turn's sandbox policy has none, `-c sandbox_workspace_write.writable_roots`
is ignored, and resume takes no `--add-dir` (verified on 0.166). Every
turn is a new Codex thread on the bot's fresh prompt, which carries the
chat thread's history; the registry keeps the last thread id and the turn
count.

## Tool-less calls

Three routing tasks make one tool-less call to a hosted model and read
its text: `tagging` (`mail_tagger`), `thread_state` (`web_thread_state`)
and `review` (the briefing's still-open check, the nightly reconcile and
the backstop on local scheduling writes). Each goes through
`preprocessor/hosted_text.py`, which asks the routing table for the
task's hosted model. On Codex the call is `codex exec` with every tool
off (`--disable` for the shell, the apps, the plugins, the hooks and the
image viewer), read-only and ephemeral, on the account's own model. A
run that executes a tool anyway counts as no answer.

- As the resident by default, in an empty working folder of its own
  (`~/.ea/state/codex-text`, mode 750), as Claude's `_run_claude` runs:
  the review and the thread states read records, not mail text.
- As the owner for the tagger, whose prompt carries the mail text: that
  text stays in the owner's processes, as it does for the tagger's Claude
  run. The owner's binary is `EA_CODEX_BIN`, `codex` on PATH, or the
  newest under `~/.nvm` (cron's PATH has none), with its own folder put
  on PATH for `node`.

A Codex call that gives no answer (a harness error, a timeout, an empty
reply, a tool call) hands the call to Claude once, and the run log says
so. `python3 preprocessor/hosted_text.py probe [--as-owner]` makes one
call.

## Not covered

Sessions (`--resume`), subagents and MCP connectors are Claude Code
mechanisms with no Codex twin here: every Codex turn is a new thread, an
email body comes from `email_writer.py` or the turn itself, and a
connector's work goes through the script that does the same job
(`drive_client.py` for Drive, `mail_actions.py` for mail). The plugin
tool grants and denials hold for a script run through `plugin_run.py`;
a script a turn runs by its own name stays inside the sandbox, with no
network. The `mail` and `drafting` tasks have no hosted choice of
their own: hosted there means the turn's own model
(`model_routing.allowed`).
