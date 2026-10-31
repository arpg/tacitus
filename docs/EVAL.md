# Evaluation & Quality Tracking

How we test LLM decision-making quality, prevent regressions, and
measure improvement over time.

## Overview

The EA system has a three-layer eval strategy:

| Layer | What it tests | Speed | Tokens | When |
|-------|--------------|-------|--------|------|
| **Unit tests** (`test_prompt_logic.py`, `test_context_export.py`) | Deterministic Python: skill detection, routing, complexity, MCP decisions, HTML parsing, packaging | ~1s, 689 assertions | 0 | Every prompt-affecting commit (post-commit hook) |
| **Integration evals — prompt-only** (`run_evals.py`) | Data integrity: does the preprocessor assemble correct briefing data for Claude? | ~5s total, 19 fixtures | 0 | Every prompt-affecting commit (post-commit hook) |
| **Integration evals — full mode** (`run_evals.py --mode full`) | End-to-end LLM output: does Claude produce the right answer given the briefing? | ~2-3 min per fixture | ~2K per fixture | Nightly cron, only if prompt-affecting files changed |

The first two layers are deterministic and use zero tokens. They run in
the post-commit hook on every commit that touches prompt-affecting files
(`skills/`, `CLAUDE.md`, `preprocessor/`, `ea-chat-bot.py`, `run-agent.sh`).

The third layer **invokes Claude** and checks the actual LLM output. It
runs nightly via cron (`tests/evals/run_full_if_changed.sh`), but only
if prompt-affecting files changed since the last successful run. This
avoids burning tokens on unchanged code while ensuring every prompt or
logic change gets a real end-to-end check within 24 hours.

## Running evals

### What runs automatically

You do not need to run these manually. The post-commit hook and
nightly cron handle it:

| Trigger | What runs | Tokens |
|---------|-----------|--------|
| `git commit` (prompt-affecting files) | Unit tests (689) + prompt-only evals (16) | 0 |
| Nightly cron (2 AM, if files changed) | Full-mode evals (19 fixtures, one Sonnet agent pass each over the full prompt) | substantial (19 agent runs; budget accordingly) |

The nightly cron is `tests/evals/run_full_if_changed.sh`. It tracks
the last-tested commit in the owner-private
`~/.ea-private/evals/last-full-commit` and skips if nothing
prompt-affecting changed. Logs go to the owner-private
`~/.ea-private/logs/eval-full-*.log`.

The toggles:
- `EA_POSTCOMMIT_TESTS=0` in the committer's environment, or
  `eval_postcommit_enabled: false` in the owner-private
  `~/.ea-private/evals.json`: skip post-commit tests (deploy still runs).
  ea-config.json is the resident's to write, so the hook never reads its
  copy of this key; an unreadable evals.json runs the tests.
- `eval_full_enabled: false` in the owner-private
  `~/.ea-private/evals.json`: skip nightly full-mode evals. The weekly
  release tests read `release_tests_enabled` from the same file. Absent
  means on; a file that is a link, is writable by another user or does
  not parse reads as off (`tests/evals/eval_gates.py`).

### What a full-mode run can reach

A full-mode run is `claude -p` with tools, as the owner. It takes nothing
from the resident's space:

- It starts in a fresh scratch directory outside any checkout, which is
  also its `EA_DIR`. The directory is made in an owner-only (0700)
  `~/.ea-eval-scratch` (`EA_EVAL_SCRATCH` names another), never under the
  system temp dir, where the resident could leave a `CLAUDE.md` that
  Claude Code loads from a parent directory. The run refuses to start when
  a directory above it is writable by another user or any memory file but
  the copied `CLAUDE.md` is on its path. The briefing and ledger come from the fixture; the
  config comes from `tests/evals/config/` (a made-up user), then the
  templates in `config/`; `CLAUDE.md` is copied from the checkout. The
  live `~/.ea/config` is never read.
- The command line (`provider.claude_command`) loads no settings file
  (`--restricted --setting-sources ""`) and no MCP server
  (`--strict-mcp-config`), offers only Bash and Read (plus Write and Edit
  on the run's own `pending-event.json` for a staging fixture), allows
  Bash only for the `ledger.py` verbs in `provider.EVAL_LEDGER_VERBS` (the
  reads, `add`, `update` and `propose`; no `close`, which archives mail,
  and no verb that takes a database path), applies `scripts/disallowed-tools.txt`, and
  runs in `--permission-mode dontAsk`, so any other call is refused.
  `--restricted` confines the file tools to the scratch directory.
- `--harness codex` is refused where this user can read the resident's
  home: Codex's sandbox confines writes, not reads.

`tests/test_eval_lane_isolation.py` pins all of this.

### Manual runs

```bash
# Fast: prompt-only, all 19 fixtures (~5s, 0 tokens)
python3 tests/evals/run_evals.py

# Full: invoke Claude for each fixture (~30-70 min, 19 Sonnet agent runs)
python3 tests/evals/run_evals.py --mode full

# Force nightly script to run regardless of changes
./tests/evals/run_full_if_changed.sh --force

# Filter by category
python3 tests/evals/run_evals.py --category regression
python3 tests/evals/run_evals.py --category hard-success

# Variance detection (run each fixture 3x)
python3 tests/evals/run_evals.py --mode full --repeat 3

# Machine-readable output for CI
python3 tests/evals/run_evals.py --json

# Verbose (show assertion details even for passing tests)
python3 tests/evals/run_evals.py --verbose
```

### Promptfoo (LLM-as-judge rubrics + web UI)

```bash
cd tests/evals
# NOTE: the promptfoo path below is NOT wired into any cron or CI;
# run_evals.py is the maintained entry point. Kept for ad-hoc use only.
npx promptfoo eval                           # prompt-only mode
npx promptfoo eval --repeat 3               # variance detection
npx promptfoo eval --filter-pattern "regression"
npx promptfoo view                           # open web UI
```

Promptfoo adds LLM-as-judge rubrics on top of the Python assertions.
Each test in `promptfooconfig.yaml` has both:
- `type: python` assertions (deterministic, from `judge.py`)
- `type: llm-rubric` assertions (LLM grades the output against a rubric)

## Test categories

### Regressions (9 fixtures)

Bugs we fixed that must never recur. Each has a `bug_ref` linking to
the commit that fixed it.

| ID | Fixture | Bug |
|----|---------|-----|
| regression-001 | `completed-with-deadline.json` | Completed items appearing as OVERDUE |
| regression-002 | `already-replied-thread.json` | Already-replied threads flagged as action items |
| regression-003 | `delta-filter-unreplied.json` | Unreplied emails dropped from delta |
| regression-004 | `normal-email-no-question.json` | Emails without `?` not surfacing |
| regression-005 | `multi-step-batch.json` | Multi-step instructions under-decomposed |
| regression-006 | `name-from-header.json` | Contact names hallucinated instead of from headers |
| regression-007 | `no-sandbox-leak.json` | Sandbox errors leaking into Slack output |
| regression-008 | `reopened-thread.json` | New replies on closed threads not surfacing |
| regression-009 | `slack-format-valid.json` | Slack mrkdwn formatting regressions |

### Hard successes (7 fixtures)

Non-obvious correct behaviors we want to preserve. If a prompt change
accidentally breaks one of these, we know we regressed.

| ID | Fixture | Behavior |
|----|---------|----------|
| hard-success-001 | `cc-only-no-reply.json` | CC-only emails treated as FYI, no reply suggestion |
| hard-success-002 | `mixed-priority-triage.json` | Priority ordering: deadline > VIP > routine CC |
| hard-success-003 | `empty-calm-day.json` | Calm day briefing is concise, no fabricated urgency |
| hard-success-004 | `single-email-no-split.json` | Simple instructions not over-decomposed |
| hard-success-005 | `zoom-not-teams.json` | Meeting creation uses Zoom, never Teams |
| hard-success-006 | `poll-reply-sender-only.json` | Poll replies use --sender-only |
| hard-success-007 | `draft-revision-no-new.json` | Draft revisions use update-draft, not create new |

## Commit hooks

Two hooks auto-track eval coverage on every commit:

### prepare-commit-msg

When you commit files that affect prompt behavior, the hook auto-appends
an `Eval-Coverage:` trailer listing the fixture IDs that test the changed
files:

```
Fix CC-only triage to skip reply suggestion

Eval-Coverage: regression-002,hard-success-001,hard-success-002
Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

The mapping lives in `.githooks/eval-coverage-map.json`. Update it when
adding new fixtures or changing which files affect which behaviors.

### post-commit

After deploying to `/home/ea-bot/ea-system/`, the hook runs the eval
suite in prompt-only mode if prompt-affecting files changed. Green
checkmark if all pass, red warning with failure details if not.

## Fixture anatomy

Each fixture is a self-contained test scenario:

```json
{
  "_meta": {
    "id": "regression-001",
    "name": "Completed item should NOT appear as overdue",
    "category": "regression",
    "bug_ref": "completed-item overdue bug (2026-04-09)",
    "description": "..."
  },
  "briefing": {
    "generated_at": "2026-04-09T14:00:00Z",
    "mode": "full-daily-delta",
    "delta_summary": { "new_inbox_messages": 2, ... },
    "new_needs_response": [ ... ],
    "open_obligations": [ ... ],
    "todays_events": [ ... ],
    ...
  },
  "ledger_entries": [
    { "id": "...", "status": "completed", "description": "...", ... }
  ]
}
```

The provider creates an isolated temp directory with:
- `briefing-latest.json` from `briefing`
- A test SQLite DB from `ledger_entries`
- Copies of real config files from `~/.ea/config/`

In full mode, Claude reads the briefing and ledger, then produces output.
In prompt-only mode, the data is assembled into JSON for assertion checks.

## Adding a new fixture

1. Create `tests/evals/fixtures/<name>.json` with `_meta`, `briefing`,
   and `ledger_entries` sections.

2. Add an assertion function to `tests/evals/judge.py`:
   ```python
   def my_new_assertion(output: str, context: dict) -> dict:
       # Prompt-only: check data integrity
       if output.startswith("{"):
           ...
       # Full mode: check Claude's text output
       ...
       return {"pass": True, "score": 1.0, "reason": "..."}
   ```
   Add it to the `ASSERTIONS` dict at the bottom of the file.

3. Map the fixture ID to its assertion in `run_evals.py`:
   ```python
   fixture_assertions = {
       ...
       "regression-010": ["my_new_assertion"],
   }
   ```

4. Add the test entry to `promptfooconfig.yaml` with both Python and
   LLM-as-judge assertions.

5. Update `.githooks/eval-coverage-map.json` to include the new fixture
   ID under the source files it covers.

6. Run `python3 tests/evals/run_evals.py --verbose` to verify.

---

## Scaffolding new fixtures from production

When something goes wrong in production, turn it into a regression test:

```bash
# From a raw briefing file (most common):
python3 tests/evals/scaffold_fixture.py \
    --briefing ~/.ea/state/briefing-latest.json \
    --name "Missed deadline in Friday briefing" \
    --assertion missed_deadline_surfaced

# From an Opik trace ID (if Opik is enabled):
python3 tests/evals/scaffold_fixture.py \
    --trace-id 019d7371-c19b-... \
    --name "CC email wrongly suggested reply" \
    --assertion cc_reply_false_positive

# Dry run (preview without writing):
python3 tests/evals/scaffold_fixture.py \
    --briefing ~/.ea/state/briefing-latest.json \
    --name "..." --assertion "..." --dry-run
```

This creates three things:
1. `tests/evals/fixtures/<name>.json` with the briefing data and metadata
2. A stub assertion in `judge.py` (always fails until you implement it)
3. An entry in `fixture_assertions` in `run_evals.py`

After scaffolding:
1. Trim the fixture to only the relevant emails/events
2. Write the assertion logic (prompt-only checks data, full mode checks output)
3. Update `.githooks/eval-coverage-map.json`
4. Verify: `python3 tests/evals/run_evals.py --verbose`

---

## Optional: Opik trace-level observability

The eval suite and unit tests are the primary quality gate. Opik is an
optional add-on for production trace browsing, quality-over-time
dashboards, and trace-to-fixture workflows.

### How it works

All three execution paths have Opik instrumentation built in via
`preprocessor/opik_trace.py`. The instrumentation is **dormant by
default**: if Opik is not installed or not running, all trace functions
return None silently. Zero overhead, no errors.

| Path | Opik project | What's traced |
|------|-------------|---------------|
| `run-agent.sh` (cron) | `ea-briefings` | Agent, delta summary, output preview, validation, duration |
| `ea-chat-bot.py` (Slack) | `ea-slack` | Message, skills, modules, complexity, MCP decision, dispatch path |
| `tests/evals/run_evals.py` | `ea-evals` | Fixture ID, assertion results as feedback scores, pass/fail |

### Requirements

Opik runs as a Docker Compose stack: MySQL, ClickHouse, Redis, MinIO,
Zookeeper, Java backend, Python backend, nginx frontend. This needs
**4+ GB of free RAM**. If your machine has 8GB or less (e.g., WSL2
with default memory limits), Opik will OOM and may destabilize the
system. Only enable it on machines with 16+ GB RAM or a dedicated
server.

### Enabling Opik

During setup (SETUP.md "Opik"), you are asked whether to enable
Opik. If you choose yes:

```bash
pip install opik
docker compose -f opik/docker-compose.yml --profile opik up -d
opik configure --use_local
```

UI at `http://localhost:5173` (accessible via Tailscale if configured).
Three projects: `ea-briefings`, `ea-slack`, `ea-evals`.

### Disabling Opik

```bash
docker compose -f opik/docker-compose.yml --profile opik down -v
docker image prune -a -f  # reclaim ~1.6GB disk
```

The instrumentation stays in the code and does nothing. Re-enable
anytime with `docker compose up`.

### What Opik adds (when running)

- **Quality over time**: assertion scores per fixture across runs
- **Regression detection**: score drops after a prompt change
- **Latency tracking**: preprocessing vs Claude invocation time
- **Trace drill-down**: click any trace to see briefing data, output, spans
- **Trace-to-fixture**: scaffold new eval fixtures from production traces

## Release tests (weekly, and when a tag is cut)

The three layers above answer "did this commit break a behavior". The
release tests answer "is the system, on the models it runs on today, as
good as it was last time". They are the expensive layer: every case
reaches a model, and the model is whichever one owns that lane now, read
from the remote-LLM switches (`remote_llm.py switches`) and the bot's
Claude default (`EA_MODEL`). Runner: `tests/release/run_release.py`.

| Lane | Cases | Who answers | What is checked |
|------|-------|-------------|-----------------|
| `briefing` | `tests/evals/fixtures/*.json` (every eval fixture, full mode) | Claude, the bot's default model | the fixture's judge assertions |
| `drafting` | `tests/release/cases/drafting/*.json` | `email_writer.py` (remote) when `drafting = remote`, else Claude with the same instructions, profile and voice guide | no em-dash, no significance clause, no LLM adverbs, required and forbidden phrases, length; then a 0-10 rubric score from the pinned judge |
| `scheduling` | `tests/release/cases/scheduling/*.json`; the solver reads the live calendars, or `tests/release/fixtures/calendar-canned.json` with `--canned-calendar` (the default with no calendar token) | the scheduling pipeline (`sched_pipeline.run` over the case's canned `pipeline.thread`, shadow) when the case carries one, else the remote scheduling agent (`sched_agent.run_agent`, writes recorded, never executed); both when `scheduling` is `shadow` or `enforce`, skipped when off | a reply, concrete times, no decline, no tool talk, no weekday mismatch, every offered poll option mentioned (`must_mention`); sender-only on a drafted poll reply. The lane is ask-first, so a proposal in chat with no draft is a pass; `"drafts_reply": true` in `expect` demands a draft. A pipeline case checks the status, the picks, the check problems and the shape: `counter`, `kind`, `hold`, `attendees`, `subject`, `starts_at`; its dates are written `{{+7d}}` (or `{{+7d:%A}}`) and land in the future on every run |

The judge model is pinned (`JUDGE_MODEL` in the runner) so a score
moves only when the model under test or the prompts move; the remote
model never grades itself.

**Scores and verdicts.** Every case gets a score in [0, 1] and a pass.
The run is stored owner-private in `~/.ea-private/release-tests/`
(`runs/<run id>.json` with outputs, `runs.jsonl` with one summary line
per run). The verdict compares the run to the newest earlier measured
run (or `--baseline <run id>`):

- `red`: a `regression` case failed. Fix it before tagging.
- `amber`: a lane's mean fell more than 0.15 below the baseline.
- `harness`: half or more of the cases errored before reaching a model
  (quota, auth, endpoint down). Not a regression; retry.
- `green`: everything else.

Exit codes: 0 green, 1 red or amber, 2 harness, 3 skipped.

**When it runs.**

- Saturday 23:00 (`crontab.master`): `tests/release/run_weekly.sh`
  runs every lane, but skips when the prompt-affecting code, both model
  ids and the switch values match the last full run. A red, amber or
  harness verdict is posted to the health channel. Toggle:
  `release_tests_enabled` in the owner-private `~/.ea-private/evals.json`.
- Cutting a tag: `scripts/cut-tag.sh v1.2.0 [-m "..."] [--push]` checks
  the branch, the tree and the CHANGELOG heading, runs the suite on HEAD
  as `--tag v1.2.0`, and tags on green or amber. Red or harness stops it
  unless `--force`. The verdict line is in the tag message (`git tag -n3`).
  Tags are for new functionality; a bug fix found during the week ships
  on main and is covered by the Saturday run.
- Marking a release stable: `scripts/cut-tag.sh --stable [vX.Y.Z]
  [--push]` on a checkout at that release (CI signs a `vX.Y.Z` tag on
  every green push to main, docs/updates.md). It verifies the tag
  against `.release-key.pub`, runs the suite on HEAD as that tag, and on
  green or amber moves `channels/stable` there, the verdict line in its
  message. Installs on the `stable` channel follow it.
- A tag push, or a manual dispatch, on GitHub:
  `.github/workflows/release-suite.yml` (next section).

**On a fresh machine.** The suite needs a Claude login and nothing else:
no mail or calendar token, no remote model, no resident user.

- *The canned calendar.* The scheduling lane's solver normally reads the
  live calendars. With `EA_CANNED_CALENDAR=<path>` set,
  `scheduling_engine.py` reads `tests/release/fixtures/calendar-canned.json`
  instead, in every verb (`propose-meeting`, `validate-draft`,
  `check-attendees`): the user's calendars, the EA-Managed rule events
  (`wh`, `nm`, `p1`), an all-day event, an EA hold, and per-attendee busy
  blocks that stand in for Graph `getSchedule` (an address the fixture
  lacks is `unknown`). Dates in the fixture are `{{+Nd}}` tokens
  relative to today, the same tokens the pipeline cases use, so the
  fixture never ages; `weekly` templates expand to one instance per
  weekday. `run_release.py --canned-calendar [PATH]` turns it on. With
  neither a Graph nor a Google token readable under `EA_DIR/auth` it is
  on by default, and the report's `calendar` line says why;
  `--live-calendar` forces the live read. In canned mode the agent runs
  the solver as the caller from the checkout (no `sudo -u ea-bot`), and
  the engine does not forward to the mail mediator: a fixture needs no
  token. The scheduling lane still needs the remote model (the
  `scheduling` switch at `shadow` or `enforce`); with the switch off the
  lane is skipped, as before.
- *The harness.* `--harness codex` runs the briefing lane through the
  Codex harness (`run_evals.run_eval(..., harness="codex")`); the default
  is `claude`. The harness is recorded in every result, in the summary
  and in the fingerprint, and a run compares only to a baseline on the
  same harness: a codex run never compares to a claude baseline, and
  `--baseline <run id>` refuses a run from the other harness. What the
  Codex run differs in (instructions embedded, the sandbox in place of a
  tool allow list, its own login and quota) is in `docs/codex-harness.md`;
  `ea-doctor.py --probe-codex` checks the binary answers.
- *The login.* On a machine with no browser, ea-bot signs in with a
  long-lived subscription token:

  ```bash
  cd / && sudo -u ea-bot env HOME=/home/ea-bot claude setup-token
  ```

  Approve it in a browser with the EA's Claude account and keep it as
  `env.CLAUDE_CODE_OAUTH_TOKEN` in `/home/ea-bot/.claude/settings.json`
  (mode 600; `docs/macos-claude-binary.md` shows the file). Claude Code
  reads it at every start. The token spends that subscription's quota:
  a full suite run is the full-mode eval suite plus the drafting cases
  and the judge calls, so run single cases with `--case` while working
  on a case. It expires about a year after it is minted; renew it the
  same way.
- *What runs on Claude.* With the remote switches off (a fresh machine
  has no `remote-llm.json`), the drafting lane runs on Claude with the
  same instructions, style profile and voice guide the remote writer
  gets, so a drafting case is measured either way. The briefing lane is
  Claude's in every configuration.

**On GitHub.** `.github/workflows/release-suite.yml` runs by hand only,
with Actions > Release suite > Run workflow (inputs: one `lane`, the
`harness`); a tag does not start it, since a run spends the login's
model quota for about an hour. It
builds the Docker image, prepares the volumes (`docker compose up init`),
writes a scratch configuration into ea-bot's config volume (the install
tests' `tests/install/answers-email.json` when present, else the
`config/examples/cu-boulder` profile with example values), installs
ea-bot's Claude login from the repository secret `CLAUDE_CODE_OAUTH_TOKEN`
(minted as above, stored under Settings, Secrets and variables, Actions),
runs `run_release.py --canned-calendar --json --tag <tag>` as ea-bot in
the `agent` service, uploads the run JSON as the artifact
`release-run-<tag>`, and fails on a `red` or `harness` verdict (`amber`
is a warning). The stack and its volumes are removed at the end. In the
container no remote model is configured, so drafting runs on Claude and
the scheduling lane is skipped; the canned calendar is in place for the
day the lane has a Claude runner.

**Adding a case.** When a bug involves the model's judgment, add a case
to the lane it belongs to, with `category: regression` and a `bug_ref`:

```bash
# briefing: scaffold from the production briefing (see above), category regression
python3 tests/evals/scaffold_fixture.py --briefing ~/.ea/state/briefing-latest.json --name "..." --assertion ...

# drafting: a brief and its checks
cat > tests/release/cases/drafting/drafting-005-<slug>.json <<'EOF'
{"id": "drafting-005", "name": "...", "category": "regression", "bug_ref": "...",
 "to": "someone@example.edu", "brief": "...",
 "must_include": [], "must_not_include": [], "max_words": 150, "rubric": "..."}
EOF

# scheduling: a typed request and what the turn must do
cat > tests/release/cases/scheduling/scheduling-004-<slug>.json <<'EOF'
{"id": "scheduling-004", "name": "...", "category": "regression", "bug_ref": "...",
 "text": "...", "expect": {"times": true, "no_decline": true, "drafts_reply": true}, "min_times": 2}
EOF

python3 tests/release/run_release.py --dry-run                 # routing and the case list
python3 tests/release/run_release.py --lane drafting --case drafting-005 -v
```

`must_include` and `must_not_include` entries are case-insensitive
substrings or regular expressions. A `quality` case counts toward the
lane mean but never turns the run red. `--repeat 3` takes the median
score and requires every repeat to pass.

Not covered yet: the remote briefing (`remote_briefing.py`) and the mail
gate's records have no canned-input runner, so a change there shows up
in the ghost-week numbers, not here.

## Summary

| Layer | What it does | Trigger | Tokens |
|-------|-------------|---------|--------|
| **Unit tests** (689 assertions) | Skill detection, routing, complexity, MCP, HTML parsing | Post-commit hook | 0 |
| **Prompt-only evals** (16 fixtures) | Prompt assembly, data integrity | Post-commit hook | 0 |
| **Full-mode evals** (16 fixtures) | End-to-end LLM output correctness | Nightly cron (if changed) | ~32K |
| **Release tests** (3 lanes, scored) | Quality against the models in effect, compared to the last run | Saturday 23:00 cron (if changed) and `scripts/cut-tag.sh` | the full-mode suite plus the drafting and scheduling cases |
| **Scaffold script** | Turn production failures into regression tests | Manual | 0 |
| **Opik traces** | Production observability, quality dashboards | Optional, needs 4+ GB RAM | 0 |
