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
- **hosted**: Claude through Claude Code, on the `opus` or `fable` tier.
- **auto**: the remote layer's switches and the `tiers` block decide, as
  they did before the table.
- **task**: one kind of model work with its own inputs, for example
  "write an email body" or "read a new message into a record". The code
  has ten: `mail`, `tagging`, `drafting`, `digests`, `briefing`,
  `audit`, `scheduling`, `chat`, `thread_state` and `review`.

## The tasks

Each task names work that one or more switches already served. `auto`
keeps that switch; `local` and `hosted` outrank it. "What the local
model sees" is what reaches the endpoint when the task runs there.

| Task | The work | The switch `auto` keeps | What the local model sees |
|---|---|---|---|
| `mail` | read each new message into a record (class, ask, deadline, people); the thread briefs on ledger rows with `mail = sealed` | `mail` (`enforce`, `ghost`, `sealed`) | every new message; each open row's thread |
| `tagging` | Outlook categories on new mail | follows the mail gate | each new message |
| `drafting` | email bodies in the user's voice | `drafting` (`remote`, `ab`) | the brief, the thread, the draft |
| `digests` | list and admissions digests | `digests` | the digest mail |
| `briefing` | the morning briefing and the afternoon update; hard items are handed to Claude one thread each | `briefings` | ledger, records, calendar |
| `audit` | the Sunday audit; flagged items go to Claude | `audits` | ledger, briefing JSON |
| `scheduling` | the scheduling channel and the proactive scheduler; every local write passes the Claude backstop | `scheduling`; `tiers.lanes` sched-channel and proactive | the thread, solver output |
| `chat` | turns in topics, rooms and the DM | `tiers.lanes` main, dm and room; `@local`, `use local` | the topic's transcript |
| `thread_state` | the state of a web chat thread the rules leave open (docs/webchat-state.md) | none: `auto` runs like `local` | the thread's messages (card titles and outcomes, never a card's body), the channel, the linked ledger rows' type, status, deadline and last action |
| `review` | the briefing's still-open check, the nightly ledger reconcile, the scheduling backstop, the hand-offs from the briefings and the audit | none: hosted only | nothing |

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
  no switch: with no local model it runs on the rules alone, and the
  doctor does not warn about its default.
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
  local|hosted|auto`. Each change is one line in `routing-changes.jsonl`
  in the remote-LLM store: the time, `via` (`web` or `cli`), what changed
  and, for a web save, the credential that signed the request in
  (`cookie` or `header`) and the device of a relayed request.

## How the lanes read it

There is no compile step and no `apply`: each lane asks the table at the
point where it already reads its switch, on its next run or turn, with
no restart. A table that cannot be read leaves each switch as it is.

| Task | Decision point | `local` | `hosted` |
|---|---|---|---|
| `mail` | `remote_llm.mode("mail")` (`model_routing.mail_mode`) | `off` and `shadow` read `enforce`; `ghost` and `sealed` stay | the gate is off |
| `tagging` | `mail_tagger._remote_mode` | the local model tags | Claude Haiku tags |
| `drafting` | `email_writer.writer_on`, `draft_ab.mode` | the writer (`remote`) | the `email-author` subagent |
| `digests` | `remote_digests.main` | the remote lane | the Claude lane |
| `briefing` | `remote_briefing.active` | the remote lane | the Claude lane |
| `audit` | `remote_audit.active` | the remote lane | the Claude lane |
| `scheduling` | `model_tiers.resolve` (lanes sched-channel, proactive); `remote_llm.mode("scheduling")` | the lane's tier is `local`; `scheduling enforce` still needs its ghost-week GO | the agent is off, and the lane's local picks run on `opus` |
| `chat` | `model_tiers.resolve` (lanes main, dm, room) | the lane's tier is `local` | a pick that would run on `local` runs on `opus` |
| `thread_state` | `web_thread_state.route` | the local model judges a thread the rules leave open | not built: the rules alone set the state, with a log line once an hour |
| `review` | `remote_briefing.audit_judge` | refused | always |

`remote_llm.py switches` shows each switch's `effective` value with the
table applied.

**Precedence in chat and scheduling turns**, strongest first:

1. the message's tag (`@local`, `@opus`, `@fable`);
2. the topic's pin (`use local`);
3. the topic's name (`[fable] ...`);
4. the table's `local` or `hosted` for the lane's task;
5. `tiers.lanes`;
6. where today's knobs put the turn: an adopted session's model, a
   room's specialist, the A/B's drawn arm, or the bot's own model.

`hosted` moves only a pick that would run on `local`; a room keeps its
specialist's model, and a `fable` lane default stays `fable`. The stakes
rule sits over every pick but the tag (below).

## The floors

Two rules in code sit under every value. No file lowers them.

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
   there would hand kept mail to Claude. While the keep list
   (`keep_from_anthropic` in `remote-llm.json`) keeps anything (a domain,
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
- **Not built:** per-task lines in the 17:30 report (local and hosted
  counts and the floor moves, from `tier-turns.jsonl` and
  `remote-llm/calls.jsonl`, sizes and timings only, never text).
- `tests/test_model_routing.py` checks the defaults against the
  template, that each lane follows the table at its decision point, that
  every floor holds against a file and a save, that `local` with no local
  model keeps every lane on Claude, that only owner processes read and
  write `routing.json`, and that a table that cannot be read leaves each
  switch as it is.

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
