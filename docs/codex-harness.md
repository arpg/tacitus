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
  `workspace-write` writes only under the working root (`-C`) and the
  `--add-dir` directories. The eval sets the root to the run's throwaway
  EA_DIR, so the test ledger and a staged event land there and the code
  tree stays read-only. Network is off inside the sandbox.
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
  read it.
- **What is replayed.** `briefing`: after the morning briefing or the
  afternoon update, run-agent.sh hands the exact prompt it gave Claude to
  `enqueue-lane`, with Claude's report as the reference. `chat`: after a
  turn Claude answered in the EA channel, the DM or a room, the bot hands
  the context block and the user's message to `enqueue` (a share of the
  turns, `--share`). Each job runs detached; the production turn never
  waits for it.
- **How a replay runs.** As the resident, never as the owner: Codex's
  sandbox confines writes, not reads, and a model shell started as the
  owner could read the owner's secrets. The working root is a scratch
  EA_DIR under `~ea-bot/.local/state/ea-lanes/codex-ghost/` holding
  copies of the ledger, the project store and the briefing, and links to
  the config, the docs and the mediator's queue. The run id starts with
  `cron-ghost-codex-`, so the mail gate treats it as a cron lane, and the
  mediator answers every verb that is not a read with `{"status":
  "recorded"}`: a draft, an archive or a snooze is written to
  `recorded-writes.jsonl` and never executed. Network is off.
- **Records and grades.** `turns.jsonl` holds Claude's output, Codex's
  output, its commands, usage and timing. `grade` runs the ghost week's
  judge (Sonnet as ea-bot, or `--judge remote`): a briefing is `same`,
  `minor` or `major` with the items missing, extra or wrong; a chat turn
  is `send`, `edit`, `wrong` or `none` with problem kinds. `report` gives
  the verdict per kind against `PASS` (GO, NO-GO, NOT ENOUGH DATA), the
  wrong and major lists and the judge's enhancement lines. `final` writes
  the verdict files that `gate` reads.
- **Quota.** `limits` reads the account's 5-hour and weekly windows
  through `codex app-server`; `enqueue` skips, and records the skip, once
  a window passes `QUOTA_STOP`.
- **Setup.** `deploy/enable-codex-ghost.sh` as the owner, once: it copies
  the owner's Codex binary into ea-bot's space, writes ea-bot's
  config.toml, signs ea-bot in with a device code and probes.

## Production

`EA_HARNESS=codex` in the environment of `run-agent.sh` runs a briefing
lane on Codex for real: `codex_ghost.py run-lane --live` as the resident
against the real EA_DIR, writes confined to its state and docs, the same
prompt and the same output handling. It runs only when `codex_ghost.py
gate briefing` finds a GO no older than 14 days (`EA_HARNESS_FORCE=1`
overrides); otherwise the Claude run goes ahead and the log says why.
The chat lanes have no production path on Codex yet (next section).

## Not covered

The chat lanes in production (the ghost replays them), the scheduling and reply lanes, sessions (`--resume`),
subagents, MCP tools and the plugin tool denials are Claude Code
mechanisms with no Codex twin here. A Codex production lane would need
the tools behind one MCP server first.
