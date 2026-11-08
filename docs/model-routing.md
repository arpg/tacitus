# Model routing: one table, task by task

- **Status:** built in part (2026-10-31). The table, its two floors, the
  web chat's Settings screen, the `routing` chat command and the doctor's
  `model routing` row are in `preprocessor/model_routing.py`, with
  `ea_config.get_routing` and `tests/test_model_routing.py`.
  [llm-lanes.md](llm-lanes.md) section 1.1 is the reference for what the
  code does. This page keeps why the table exists and what it means on a
  Mac. A part marked **Not built** is design the code does not have.
- **Asked for by:** the user who asked for the macOS native backend
  ([macos-native-accounts.md](macos-native-accounts.md)): with mail and
  calendars kept on the Mac, the next question is which model reads them.
- **Builds on:** the remote layer's switches ([llm-lanes.md](llm-lanes.md)
  section 1), the chat tiers (`model_tiers.py`, llm-lanes section 2.16)
  and the stakes rule.

## Words

- **local**: the OpenAI-compatible endpoint in `<private dir>/remote-llm.json`
  (`remote_llm.py`), the `local` tier in chat. "Local" means the owner
  chose where it runs: on the Mac itself, on the owner's own server, or
  on a colleague's. The endpoint must be https; a model on the same Mac
  sits behind a TLS proxy (`tailscale serve`, Caddy).
- **hosted**: a hosted model: Claude through Claude Code (the `opus` or
  `fable` tier), or Codex through the Codex CLI (the `codex` tier) for a
  task with a Codex route. One switch, `hosted_default`, picks it for
  every task, and the `hosted` map overrides it per task ("The hosted
  model: one switch" below).
- **auto**: the remote layer's switches and the `tiers` block decide, as
  they did before the table.
- **task**: one kind of model work with its own inputs, for example
  "write an email body" or "read a new message into a record". The code
  has ten: `mail`, `tagging`, `drafting`, `digests`, `briefing`,
  `audit`, `scheduling`, `chat`, `thread_state` and `review`.

## The tasks

Each task names work that one or more switches already served. `auto`
keeps that switch; `local` and `hosted` outrank it. "What the local
model sees" is what reaches the endpoint when the task runs there. The
Settings screen names each task by its plain label (`TASKS` in
`model_routing.py`); the key is the name in `routing.json` and on the
command line.

| Task | On the Settings screen | The work | The switch `auto` keeps | What the local model sees |
|---|---|---|---|---|
| `mail` | Reading your mail | read each new message into a record (class, ask, deadline, people); the thread briefs on ledger rows with `mail = sealed` | `mail` (`enforce`, `ghost`, `sealed`) | every new message; each open row's thread |
| `tagging` | Sorting mail | Outlook categories on new mail | follows the mail gate | each new message |
| `drafting` | Writing replies | email bodies in the user's voice | `drafting` (`remote`, `ab`) | the brief, the thread, the draft |
| `digests` | Mail digests | list and admissions digests | `digests` | the digest mail |
| `briefing` | Your briefing | the morning briefing and the afternoon update; hard items are handed to Claude one thread each | `briefings` | ledger, records, calendar |
| `audit` | The weekly check | the Sunday audit; flagged items go to Claude | `audits` | ledger, briefing JSON |
| `scheduling` | Finding meeting times | the scheduling channel and the proactive scheduler; every local write passes the Claude backstop | `scheduling`; `tiers.lanes` sched-channel and proactive | the thread, solver output |
| `chat` | Talking with you | turns in topics, rooms and the DM | `tiers.lanes` main, dm and room; `@local`, `use local` | the topic's transcript |
| `thread_state` | Noticing what needs you | the state of a web chat thread the rules leave open (docs/webchat-state.md) | none: `auto` runs the local model, and the hosted model takes the threads left when it does not answer | only threads the owner has written in: the thread's messages (card titles and outcomes, never a card's body), the channel, the linked ledger rows' type, status, deadline and last action |
| `review` | Checking its own work | the briefing's still-open check, the nightly ledger reconcile, the scheduling backstop, the hand-offs from the briefings and the audit | none: hosted only | nothing |

Letters have no task: the `email-author` subagent writes them, whatever
`drafting` says.

## The routing table

One owner-private file, `<private dir>/routing.json` (0600). It holds
one key, `tasks`:

```json
{
  "tasks": {
    "mail": "local",
    "tagging": "local",
    "drafting": "local",
    "digests": "local",
    "briefing": "local",
    "audit": "hosted",
    "scheduling": "local",
    "chat": "auto",
    "thread_state": "local",
    "review": "hosted"
  }
}
```

- **Values** are `local`, `hosted` or `auto`. A task the file does not
  name takes its default: `auto` for every task but `review`, which is
  `hosted`, and `thread_state`, which is `local`. So an install with no
  file runs as it did before the table. `thread_state` is new work with
  no switch: with no local model its `local` reads `hosted` (one Claude
  call a tick at most, none when no thread is due), and the doctor does
  not warn about its default.
- **`local` with no endpoint** (`remote-llm.json` names no endpoint and
  model) reads `hosted`, as `@local` does in chat ("no local model set
  up" in the footer).
- **Only owner processes read it.** `ea_config.get_routing` takes the
  table from `routing.json` through `owner_trust.owner_routing_block`
  and nowhere else. The `routing` block in `ea-config.json` is the
  template's copy and shapes only what the resident shows: that file is
  the EA user's to write, and the table moves mail between models. A
  `routing.json` that fails the owner checks, or is not a JSON object,
  reads as the defaults, and the doctor's row FAILs.
- **Unknown tasks and values** are dropped on read (`routing_from_block`).
  `model_routing.py set` and the Settings screen refuse them with the
  reason (`routing_problems`).
- **Changing it.** The web chat's Settings screen (owner only, never on
  a relayed copy) or, as the owner, `model_routing.py set <task>
  local|hosted|auto` and `model_routing.py set-hosted` (below). Each
  change is one line in `routing-changes.jsonl` in the remote-LLM store:
  the time, `via` (`web` or `cli`), what changed (`hosted_default` for
  the switch, `hosted.<task>` for an override) and, for a web save, the
  credential that signed the request in (`cookie` or `header`) and the
  device of a relayed request.

## The hosted model: one switch

Moving from Claude to Codex and back is one value, not a sweep of the
tasks. `routing.json` holds the switch beside the tasks:

```json
{
  "tasks": {"chat": "auto", "briefing": "auto"},
  "hosted_default": "codex",
  "hosted": {"review": "claude"}
}
```

- **`hosted_default`** (`claude` or `codex`) is the hosted model of every
  task that runs hosted, by its value or by `auto`. Flip it and every
  task without an override moves together: the chat and scheduling lanes
  (their default tier), the briefings, the audit and the digests
  (`run-agent.sh` asks `model_routing.py harness`), and the tool-less
  calls of the tagger, the thread states and the review (`hosted_text`).
- **`hosted`** holds the overrides: a task the owner set apart keeps its
  model when the switch moves. It is empty by default. "Use default" on
  the Settings screen, or `set-hosted --clear-task <task>`, drops one.
- **A task a model cannot take** reads Claude whatever the switch says:
  `mail` hosted is the gate off and `drafting` hosted is the turn's own
  model, so neither has a hosted model of its own. `show` and the screen
  mark such a task `fixed` and say why.
- **No switch set** (the key absent, as on an install from before it):
  every task without an override runs on Claude, and the chat and
  scheduling lanes keep a `codex` lane default from the `tiers` block, as
  before. Once the switch is set it outranks that lane default.
- **A table from before the switch.** The earlier Settings screen saved
  a model for every task. When `hosted_default` is absent and the file
  names a model for every task that has a choice, the switch is the one
  model they all name, or, when they name two, the `chat` entry's model
  (the one the owner talks to); each task whose entry differs from it
  stays an override, so no task moves. The next save writes the file in
  that shape. A file that names only some tasks keeps its entries as
  overrides and sets no switch.

```text
$ python3 /opt/ea-system/preprocessor/model_routing.py set-hosted codex
{"hosted_default": "codex", "overrides": {}, "hosted": {...}}
$ python3 /opt/ea-system/preprocessor/model_routing.py set-hosted claude --task review
$ python3 /opt/ea-system/preprocessor/model_routing.py set-hosted --clear-task review
$ python3 /opt/ea-system/preprocessor/model_routing.py show
*Hosted model:* Codex for every task not set apart.
- *Your briefing* (`briefing`): auto, hosted on Codex (default), now Codex (...)
- *Checking its own work* (`review`): hosted, hosted on Claude (override), now Claude (...)
- *Reading your mail* (`mail`): auto, hosted on Claude (fixed: it has no hosted model of its own to pick), ...
```

`set <task> claude|codex` still writes one override.

**Which model a chat turn runs on**, strongest first. The same order
holds in the scheduling channel and the proactive scheduler, with the
`scheduling` task in place of `chat`.

| | Source | Scope | Example |
|---|---|---|---|
| 1 | a tag on the message | that turn | `@codex`, `@claude` (Opus), `@fable`, `@local` |
| 2 | a pin on the topic | the topic | `use codex`, `use claude`, `escalate`, `hand back` |
| 3 | the topic's name | the topic | `[codex] budget plan` |
| 4 | the task's value `local` | the task | `set chat local` |
| 5 | a `local` lane default | the lane | `tiers.lanes.dm = "local"` |
| 6 | the task's hosted override | the task | `set-hosted codex --task chat` |
| 7 | the switch | every task | `set-hosted codex` |
| 8 | a `codex` lane default | the lane | `tiers.lanes.main = "codex"`, only with no switch and no override |
| 9 | today's knobs, on Claude | the turn | an adopted session's model, a room's specialist, the scheduler model, the A/B's drawn arm, `EA_MODEL` |

Under Claude (6 or 7) a `fable` lane default stays `fable` and a room
keeps its specialist's model; under Codex they run on Codex. The task's
value `hosted` moves a pick that would run on `local` (5, the A/B draw,
a local session) to the hosted model 6 or 7 names; the stakes rule and a
missing local model do the same. `EA_MODEL` and `tiers.models.opus`
name the Claude model a Claude turn runs on; neither picks between
Claude and Codex.

Over every row sits the chat floor (below): while it holds, a pick that
would run a chat or scheduling turn on the owner's own model (rows 1 to
5, an A/B draw, a local session) runs on the hosted model rows 6 to 8
name, and `model_tiers.resolve` says so with `floor: "local_chat"`.

## Presets

A preset is a whole table under a name, the owner's stash of settings:
the task values, the switch and the overrides. `apply` writes a
preset's task values and its switch (a preset without a switch leaves
it as it is) and makes its overrides the whole override map, so a
built-in clears every override. `save` keeps the table in force under a
name in `routing.json`'s `presets`; a later save keeps them.

| Preset | On the screen | Task values | Switch |
|---|---|---|---|
| `private` | Private | `auto` for mail, tagging, drafting, digests, briefing and audit (the remote switches put them on the own model where it is set up for them), `local` for thread_state, `hosted` for chat, scheduling and review | unchanged |
| `claude` | All Claude | `hosted` for every task, `local` for thread_state | `claude` |
| `codex` | All Codex | `hosted` for every task, `local` for thread_state | `codex` |

The built-ins are `BUILTIN_PRESETS` in `model_routing.py`, defined in
code and never written; a saved preset cannot take their names. Under
the keep floor a preset's `hosted` on mail, tagging or digests is
written as `auto`, and the answer says so in a line; a value another
floor refuses (the chat floor, the stakes floor) refuses the whole
preset.

```text
$ python3 /opt/ea-system/preprocessor/model_routing.py preset list
private: Private. Mail, replies, digests, your briefing and the weekly check go to ...
claude (now): All Claude. Claude does every task. Your own model notices what needs you.
codex: All Codex. Codex does every task. Your own model notices what needs you.
$ python3 /opt/ea-system/preprocessor/model_routing.py preset save "Travel week" --note "on the road"
$ python3 /opt/ea-system/preprocessor/model_routing.py preset apply codex
```

The Models page has a Presets row at its top: a button for each
preset, the one the table matches pressed (a saved preset before a
built-in that makes the same table), and "Save current as...", which
asks for a name in the browser's own box. A pressed button is sent
alone on Save (`{"preset": "<name>"}`), and "Save current as..." sends
`{"preset": {"save": "<name>"}}`; the rows redraw from the answer. The
change log line of an applied preset names it (`preset`), and a saved
one is `presets.<name>`.

## Adding a hosted model

The policy code never names a hosted model. It asks `model_routing` by
class: `is_hosted(name)`, `is_local(name)`, `is_base(name)` (Claude, the
model the bot's own harness runs and every fallback goes to) and
`for_model(name)`, which finds a registry entry from a key, a chat tier,
an arm or a recorded model id. A new hosted model is:

1. **One entry in `ea_config.HOSTED_MODELS`:** its name, its harness,
   its chat tier, its arm, the prefixes of its model ids and the routing
   tasks it can take. The tiers, the aliases (`@<tier>`, `use <tier>`,
   `[<tier>]`), the footer's name, the A/B arms, the Settings choices,
   the switch and the validation take it from there.
2. **A harness, when it runs on a program that has none yet:** a turn
   runner in the bot's `_HARNESS_TURNS`, a tool-less call in
   `hosted_text.CALLS`, a lane runner in `run-agent.sh`, and a line in
   `HARNESS_LIMITS` when the harness cannot do something in chat. A
   model on the Codex CLI reuses Codex's harness and needs none of it.

`tests/test_hosted_switch.py` adds a made-up model with one entry and
checks that each of those places takes it. It also scans the production
code for a branch on a hosted model's name: a new one fails the test
unless its file is in the test's allowlist with the reason (a harness
file, or a record that labels the hosted side of a measure).

## How the lanes read it

There is no compile step and no `apply`: each lane asks the table at the
point where it already reads its switch, on its next run or turn, with
no restart. A table that cannot be read leaves each switch as it is.

| Task | Decision point | `local` | `hosted` |
|---|---|---|---|
| `mail` | `remote_llm.mode("mail")` (`model_routing.mail_mode`) | `off` and `shadow` read `enforce`; `ghost` and `sealed` stay | the gate is off |
| `tagging` | `mail_tagger._remote_mode` | the local model tags | the hosted model tags: Claude Haiku, or Codex as the owner (`hosted_text`) |
| `drafting` | `email_writer.writer_on`, `draft_ab.mode` | the writer (`remote`) | the `email-author` subagent |
| `digests` | `remote_digests.main` | the remote lane | the Claude lane |
| `briefing` | `remote_briefing.active` | the remote lane | the Claude lane |
| `audit` | `remote_audit.active` | the remote lane | the Claude lane |
| `scheduling` | `model_tiers.resolve` (lanes sched-channel, proactive); `remote_llm.mode("scheduling")` | the lane's tier is `local`; `scheduling enforce` still needs its ghost-week GO | the agent is off, and the lane's local picks run on `opus` |
| `chat` | `model_tiers.resolve` (lanes main, dm, room) | the lane's tier is `local` | a pick that would run on `local` runs on `opus` |
| `thread_state` | `web_thread_state.route` | the local model judges a thread the rules leave open, one thread a call (`auto`: the hosted call takes the threads left when it does not answer) | one tool-less hosted call a tick (`hosted_text.run`, as the reconcile and the backstop: Claude with the `thread_state` model in `models`, else opus, or Codex) over up to 20 threads, one JSON array back; none when no thread is due; a failure keeps the states and logs once an hour |
| `review` | `remote_briefing.audit_judge` | refused | always, on Claude or Codex (`hosted_text`) |

`remote_llm.py switches` shows each switch's `effective` value with the
table applied.

**Precedence in chat and scheduling turns:** the table in "The hosted
model: one switch" above, the one place it is written. `hosted` moves
only a pick that would run on `local`. The stakes rule sits over every
pick but the tag (below).

## The floors

Three rules in code sit under every value. No file lowers them.

0. **The chat floor, for now.** `chat` (the lanes main, dm and room) and
   `scheduling` (sched-channel and proactive) never run on the owner's
   own model while `local_chat_ready` in `ea-config.json` is false, its
   default. `set chat local`, `set scheduling local` and a preset that
   names either `local` are refused with one line; an `@local` tag or a
   `use local` pin on those lanes answers with the same line, pins
   nothing and runs the turn on the hosted model; a lane default, the
   table's `local` and an A/B draw (the on-prem arm, the pipeline) run on
   the hosted model too, and the scheduling agent's `enforce` reads
   `shadow`. `model_tiers.resolve` carries `floor: "local_chat"` (and the
   note `local_chat`, which the footer says as "your own model: not yet"),
   and the bot logs it. The Settings screen disables "Your own model" on
   those two rows and shows the line under them; `show` says "your own
   model: not yet" on them. The line, in plain words:

   > Your own model does not hold up in conversation yet. Talking with you
   > and finding meeting times stay on Claude or Codex for now.

   Setting `local_chat_ready` to true lifts it once the local model holds
   up in conversation.

1. **The stakes floor.** `review` is hosted only: `routing_from_block`
   gives it `hosted` whatever the file says, and a save of `local` is
   refused. In chat and scheduling turns the per-message stakes rule
   (`model_tiers.STAKES_RE`: money, a budget, a contract, a legal matter,
   a dean or a program officer, a decline, tenure, personnel, a
   complaint) moves a `local` pick to `opus`, as it does a lane default.
   The user's own `@local` tag stays the one exception. The local
   briefing keeps its own rails in `remote_briefing`: no draft to a dean
   or a program officer, on money, a contract or a legal matter, or for a
   decline; each becomes a decision for the user.
   **Not built:** the stakes rule on every task. A draft the local writer
   is asked for, and a briefing item, that match the stakes rule are not
   moved to the hosted model by the table.
2. **The keep floor.** `mail`, `tagging` and `digests` read new mail in
   an owner process (`digests` the list and admissions mail), so `hosted`
   there would hand kept mail to a hosted model, Claude or Codex. While the keep list
   (`keep_from_frontier` in `remote-llm.json`, read under its old name
   `keep_from_anthropic` too) keeps anything (a domain,
   an address, or the admissions rule, which is on unless it says
   `"admissions": false`), `hosted` on any of the three reads `auto`, and
   a save of `hosted` is refused. The mail gate and the mediator's kept
   checks then keep that mail from every other task, as before the
   table. An install with no local model keeps the admissions rule on
   too, so its Settings screen offers `local` and `auto` for these
   three; `auto` runs them on Claude there, as before.

**When both apply** (a contract question from a kept domain), neither
model decides: the local model writes its record, and the item goes to
the user as a decision with that record only, the way a kept message
that asks for something becomes a `kept-` ledger row. **Not built** as a
routing rule; the `kept-` row and the briefing's rails are the paths
today.

## Visible, and checked

- The doctor's `model routing` row (its own row, beside "remote LLM")
  names the tasks on each model now (local, mixed, hosted), the local
  model's state from its breaker and its last call, and where the table
  came from. It WARNs on a task set to `local` with no local model and on
  a local model that does not answer, and FAILs on a refused
  `routing.json`. **Not built:** the floor moves of the past week in that
  row.
- The `routing` chat command prints each task's value, the model it runs
  on now and why.
- **Plain words.** The Settings screen, the `routing` chat command and
  `model_routing.py show` give each task's label and line, why it runs
  where it runs, the floors and every refusal in plain words: "your own
  model" for local, the hosted model's name ("Claude", "Codex") for
  hosted, and no term of the code. Each task's choice on the screen
  reads "Your own model", the task's hosted model by name (Claude or
  Codex) and "Auto"; the words "Local" and "Hosted" are the code's.
  `tests/plain_words.py` lists the words the screen never uses, which
  the app's own text avoids too, and `tests/test_routing_presets_floor.py`
  holds the presets, the chat floor's line and the choices to it.
  `show --verbose` and the doctor keep the code's terms:
  the switches, the lanes and the local model's last error. `set` prints
  a refusal in those terms.
- **Not built:** per-task lines in the 17:30 report (local and hosted
  counts and the floor moves, from `tier-turns.jsonl` and
  `remote-llm/calls.jsonl`, sizes and timings only, never text).
- `tests/test_model_routing.py` checks the defaults against the
  template, that each lane follows the table at its decision point, that
  every floor holds against a file and a save, that `local` with no local
  model keeps every lane on Claude, that only owner processes read and
  write `routing.json`, that a table that cannot be read leaves each
  switch as it is, and that every string the Settings screen can show is
  plain.

## What the table adds over the switches

- **One place.** The choice was spread over the switches in
  `remote-llm.json` and the `tiers` block of `ea-config.json`; the table
  names tasks the way the owner thinks of them.
- **Judgment stays hosted.** `review` cannot be moved to the local model,
  and the stakes rule sits over a local table value in chat and
  scheduling turns. Drafts and briefing items: not built (above).
- **The keep floor on the tasks that read new mail.** `mail`, `tagging`
  and `digests` cannot go to Claude while the keep list keeps anything,
  the admissions rule included.
- **No change until the owner sets a value.** Every task but `review`
  defaults to `auto`, so a new install and an install with its switches
  set both run as before. The design's `hosted` default for a task with
  no entry was not built; a task added later takes the default
  `ea_config.DEFAULTS` gives it.
- **One switch between hosted models.** Claude to Codex and back is
  `hosted_default`, with the per-task overrides kept across a flip; no
  lane needs an edit when the owner swaps them.

What it does not add: a model choice inside a task (that stays the
switches and the tiers), or any change to what the mediator lets either
model do. The gates, the buttons and the mediator are the same for both.

## On a Mac

The `local` endpoint can be the Mac itself: an OpenAI-compatible server
(llama.cpp, Ollama or an MLX server) on loopback, behind `tailscale serve`
or Caddy for https, then `remote_llm.py init --endpoint https://...`. A
27B model at 4-bit quantization is about 15 GB of weights, so 32 GB or
more of unified memory. With the native backend
([macos-native-accounts.md](macos-native-accounts.md)) and every task but
`review` set to `local` in the Settings screen, mail and calendars stay
on the Mac. Claude then sees only the `review` work, letters, the stakes
turns and the owner's own `@opus` or `@fable` picks, and the mail gate
and the mediator's kept checks keep the keep list's mail from all of
them.
