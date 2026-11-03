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
  off inside the sandbox.
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
  read against the user's words, not a room's preamble): the sandbox has
  no network. A replay whose commands still hit the missing network is
  recorded as skipped.
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

The owner picks Codex per task on the web chat's Models page (the
routing table's hosted model, docs/llm-lanes.md section 1.1): `chat` and
`scheduling` then run their lanes on the `codex` tier, and `briefing`,
`audit` and `digests` run their Claude lane on Codex as below, with no
gate (`model_routing.py harness`).

`EA_HARNESS=codex` in the environment of `run-agent.sh` runs a briefing
lane on Codex for real: `codex_ghost.py run-lane --live` as the resident
against the real EA_DIR, writes confined to its state and docs, the same
prompt and the same output handling. It runs only when `codex_ghost.py
gate briefing` finds a GO no older than 14 days (`EA_HARNESS_FORCE=1`
overrides); otherwise the Claude run goes ahead and the log says why.

**Chat turns** (`preprocessor/codex_turn.py`): the arm `codex` runs a
chat turn on Codex as the resident. The bot builds the turn's fresh
prompt as it does for Claude, and `run_turn` sends it, behind what a
Claude run would load by itself, to a new Codex thread (`codex exec`;
see "No resume" below), with writes confined to the folders a Claude
turn writes and the run id the bot minted, so drafts and staged events
surface as after a Claude turn. A topic gets the arm with `use codex`, one message with
`@codex`, a lane with `tiers.lanes` in ea-config.json; the footer says
"EA · Codex". A harness error or an empty answer hands the turn to
Claude. The same function with `live=False` is the ghost's chat path:
the production path with posting off (a scratch EA_DIR per thread, the
real path replaced in the prompt, the mediator recording every write,
the staged files read back for the judge). The email-author subagent
has no Codex twin yet: a turn that needs it uses `email_writer.py`
(the remote writer) or writes the body itself.

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
throwaway EA_DIR.

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

**No resume.** `codex exec resume` drops the writable roots: the resumed
turn's sandbox policy has none, `-c sandbox_workspace_write.writable_roots`
is ignored, and resume takes no `--add-dir` (verified on 0.166). Every
turn is a new Codex thread on the bot's fresh prompt, which carries the
chat thread's history; the registry keeps the last thread id and the turn
count.

## Not covered

The chat lanes in production (the ghost replays them), the scheduling and reply lanes, sessions (`--resume`),
subagents, MCP tools and the plugin tool denials are Claude Code
mechanisms with no Codex twin here. A Codex production lane would need
the tools behind one MCP server first.
