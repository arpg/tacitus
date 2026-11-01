# Remote LLM layer (optional)

> The parts now in place (the mail gate, the email writer, the remote
> digests, the briefings), their switches, worked examples and rollback are in
> **[docs/llm-lanes.md](llm-lanes.md)**. This file is the design history:
> the shadow run, its measures, the tuning harness and the A/B test.

An optional, owner-side layer that runs some EA work on an outside
OpenAI-compatible model instead of Claude. Its purpose is data
sovereignty: less of the inbox and calendar going to Anthropic. It ships
off and belongs to no install profile. The one part `/setup` offers is
the email writer (`drafting`), as the last optional screen; the rest is
switched on by the owner after reading `docs/llm-lanes.md`.

## With or without a remote model

This section is for a user who decides whether to run one. The rest of
this file is design history.

### Without a remote model (the default)

A fresh install has no `remote-llm.json`, so every switch reads `off`. A
file with an endpoint and every switch at `off` gives the same result.
Each lane then takes its Claude path:

| Lane | With the remote model off |
|---|---|
| Briefings | `remote_briefing.py` exits 3 and `run-agent.sh` runs the Claude lane. |
| List and admissions digests | `remote_digests.py` hands the run to `run-agent.sh`, which runs it on Claude. |
| Email bodies | `email_writer.py` exits with its fallback code, and the `email-author` subagent writes the body. |
| Mail tagger (when it is on) | One Claude call per batch. |
| Mail gate | Nothing is gated. Claude lanes read the message text. |
| Scheduling | The scheduling channel and the proactive scheduler run on Claude. |
| `@local`, `use local` | The turn runs on Opus. The footer says "(no local model set up)". |

`tests/test_remote_off.py` checks each row.

### Any OpenAI-compatible endpoint

The layer speaks the OpenAI chat-completions API. It works with:

- vLLM (`vllm serve <model>`);
- Ollama (its `/v1` API);
- a hosted provider that serves open-weight models.

Two rules:

1. **The endpoint must be https.** The config refuses an `http://` URL,
   also on this machine. Mail travels to the endpoint, and on loopback
   another local user could listen on the port first. For a local server,
   put a TLS proxy in front of it, for example `tailscale serve` or Caddy.
2. **The key goes in the owner-private secrets file**, as
   `REMOTE_LLM_API_KEY=` in `~/.ea-private/secrets.env`.

Then set it up and test it:

```bash
python3 preprocessor/remote_llm.py init --endpoint https://<host>/v1 --model <name> --drafting remote
python3 preprocessor/remote_llm.py ping
```

`init` turns on the email writer only. The mail, digest, briefing and
scheduling switches are separate decisions (`docs/llm-lanes.md`).

### The privacy trade-off of a hosted endpoint

The remote model reads everything its switches give it. With `mail` at
`enforce` that is every new message. A hosted endpoint therefore moves
your mail to that provider instead of to Anthropic. Choose it by its
contract (no retention, no training on your data) and by where it runs.
A model on a machine you control keeps the mail with you; that is the
case this layer was built for. The `keep_from_anthropic` list limits
Claude only, never the remote model.

### The sizing cost of running without one

With the remote model off, these lanes come back to Claude and use your
plan's limits:

- **The briefings:** two runs each weekday, each with its own audit
  pass. On one heavy install they cost about $2.50 a day at API list
  prices.
- **The digests:** one run per digest mail.
- **The email bodies:** the `email-author` subagent writes each one
  inside the chat turn that asked for it.
- **The mail tagger, when it is on:** one call every two minutes while
  mail arrives.
- **The mail gate:** Claude lanes read the full message text, so each
  turn carries more context.

Chat turns are most of the use either way. The remote model changes the
background load, not the conversations you start.

## The parts

Two parts, each behind its own switch:

| Part | Module | What it does |
|---|---|---|
| `mail` | `preprocessor/mail_privacy.py` | Extracts each new message into a fixed record (sensitivity class, ask, deadline, scheduling content, people and roles, tone, Outlook state and domain tags, summary). Builds a calendar shim and a reduced briefing from those records. |
| `scheduling` | `preprocessor/sched_agent.py` + `skills/sched-agent.md` | A scheduling agent that drives the same tools as the scheduling lane (the OR-tools solver, `reschedule-brief`, `check-attendees`, booking links, polls, calendar reads) through a JSON step protocol. |

The shared client and switchboard is `preprocessor/remote_llm.py`.

Two pieces sit beside the parts. `preprocessor/tool_gateway.py` is the
tool gateway: the endpoint serves the model with native tool calling
off, and the gateway, on loopback as the owner, renders a harness's tool
list into the model's own tool prompt and parses the `<tool_call>`
blocks back, under the Anthropic Messages API (Claude Code through
`ANTHROPIC_BASE_URL`) or the OpenAI chat API (opencode, Qwen Code).
`sched_agent.backstop_review` is the Claude backstop on the scheduling
agent's live writes: every draft, staged event or hold it makes is
reviewed before the user sees a button, and a withheld turn is undone
and handed to Claude. Both in `docs/llm-lanes.md` §2.14. On top of
them `preprocessor/model_arms.py` runs the model A/B (§2.15): a chat
topic draws an arm on its first message, `claude` or `remote` (the same
Claude Code harness on the on-prem model through the gateway), keeps it,
and every turn, Send, Cancel and calendar button in it is scored per
arm; `model_arms.py enable` opens the window, `report` is the
scorecard, `which model` and `use claude` work in any topic.

## Modes

A switch reads `off` or `shadow`; the mail switch can also read
`enforce` or `sealed`, and the scheduling switch `enforce`.

- **off**: every verb, cron and hook returns at once. Nothing is sent.
- **shadow**: the remote model runs beside the live Claude lanes and
  records what it would have done. Claude's inputs and actions do not
  change. Nothing the shadow does is posted, sent, staged or written to
  the mailbox or calendar.
- **enforce** (mail only): the remote model reads every message first
  and Claude's bulk lanes see only what the mail gate lets through. See
  "Enforce" below.
- **ghost** (mail only): enforce, plus a dated week in which every
  lane's read is sealed on the side and recorded, so a judge can grade
  what `sealed` would have cost before it is set (`ghost_week.py`,
  section 2.13 of `docs/llm-lanes.md`). Past its window it reads enforce.
- **sealed** (mail only) and **enforce** for scheduling are the
  cutovers; `remote_llm.py set` refuses each without the ghost week's GO.

A shadow switch reads `off` after `shadow_until`, and one with no end
date reads `off`: a shadow run always stops by itself. `enforce` has no
end date; it stays until the switch is changed. `set <switch> <value>
--days N` opens the ghost window for `mail ghost` and `scheduling
shadow` (and moves `shadow_until` for the latter).

## Config and trust

`<private_dir>/remote-llm.json` (owner-only, ea-bot cannot write it):

```json
{"endpoint": "https://llm.example.edu/v1",
 "model": "your-model-name",
 "mail": "shadow", "scheduling": "shadow",
 "shadow_until": "2026-10-26",
 "keep_from_anthropic": {"domains": ["mil", "contractor.example.com"],
                         "addresses": [], "admissions": true}}
```

- The key is the owner secret `REMOTE_LLM_API_KEY` in the private
  `secrets.env` (`remote_llm.py init` writes the file). Only owner processes read it; `remote_llm.chat()`
  refuses in any other process. `scripts/set-secret.sh REMOTE_LLM_API_KEY`
  stores it; no other name for the key is read.
- The endpoint must be https. The client checks that each answer names
  the configured model.
- **The remote endpoint receives every message.** Nothing gates what goes
  to it.
- **`keep_from_anthropic`** names the mail that should, in an enforce
  mode, reach only the remote model and never Claude. A domain covers its
  subdomains (`mil` covers `army.mil`). For mail, every participant (from,
  to, cc) is checked; with `admissions` on, a message from outside the
  institution that the admissions filter would move counts too. In shadow mode the list
  only labels: each extraction record carries `keep_from_anthropic` with
  the reason, the reduced briefing replaces such a thread with a stub (no
  subject, no preview: only that it needs a reply and by when), the
  release log counts each time Claude read one, and the scheduling agent
  labels a turn whose task text or tool output names a listed address.
- The scheduling agent's tools run **as ea-bot** through
  `sudo -n -u ea-bot`, so mail and calendar reads go through the mail
  mediator exactly as Claude's do. The agent has Claude's privileges and
  none of the owner's. Its writes (drafts, staged events, holds, poll
  fills, decision logs) are validated and recorded, never run.
- The store is `<private_dir>/remote-llm/` (0700, files 0600). It holds
  the extraction records, the shadow turns and transcripts, the release
  log and the reports. `calls.jsonl` records sizes, timings and errors
  of every request, never prompt or completion text.
- All requests from every process share one lock, so the crons and the
  agent never stack calls on the server. A circuit breaker pauses calls
  after three failures in a row.
- `<private_dir>/routing.json` (owner-only) is the routing table over
  these switches: per task `local`, `hosted` or `auto`, with the stakes
  and keep floors in code (`docs/llm-lanes.md` 1.1).

## The shadow run

| What | Where | When |
|---|---|---|
| Extract new mail | `mail_privacy.py tick` | owner cron, every 10 min |
| Reduce each new briefing | `mail_privacy.py shadow-briefing` | owner cron, weekdays at :35 |
| Claude sample: the briefing prompt on the full and on the reduced briefing (Read only, nothing posted), plus a judge's comparison | `mail_privacy.py claude-sample` | Tue and Thu at :50, once a day |
| Re-run each scheduling turn | `sched_agent.py run-job` | spawned by the bot after every Claude turn whose modules include scheduling (scheduling channel, proactive requests, scheduling turns elsewhere); not behind a remote-arm turn Claude redid, whose own row is the measure (llm-lanes.md, section 2.15) |
| Record which threads a mail read handed to a lane | `mail_privacy.note_release` | the mediator, after it answers a read verb |
| Record Claude's staged events and the user's button outcomes | `sched_agent.note_claude_staged`, `note_event_outcome` | the bot |
| Numbers to the health channel | `mail_privacy.py report --post` | daily 17:30 |
| Final report and grading sheets (private markdown) | `mail_privacy.py final-report` | posted by the report on `shadow_until` |

The report's header names each switch and the date its window ends. A
line whose measure is not running is left out: the briefing reduction
outside `mail = shadow`, the drafting A/B unless `drafting = ab`, and
the shadow scheduling counters while the ghost week reports the same
turns over its window.

`mail_privacy.py backfill --days N` extracts older mail once.
`mail_privacy.py redo-withheld` re-extracts mail an earlier version held
back from the remote model.

### What is measured

Mail:
- needs-reply recall and precision against the messages the user
  answered within three days;
- ledger recall (rows created from a thread the shadow saw) and deadline
  recall and same-date rate;
- state and domain tag agreement with `mail_tagger` (Haiku, or the
  remote model itself under `enforce`: then the measure is the record's
  per-message state against the tagger's, two prompts on one model);
- confidential share and kinds;
- how much mail the keep_from_anthropic list covers, by reason;
- chat reads of a thread the shadow classed confidential or kept from
  Anthropic (the reads an enforce mode would block or gate);
- briefing size before and after, previews replaced;
- latency, cold starts, errors, characters sent.

Scheduling (both sides scored the same way):
- invented times (shadow only: a time not inside a solver window of that
  turn; must be zero);
- validity of every named time per a fresh `validate-draft`;
- rule breaks (dashes, `**`, Teams, weekday/date mismatch, declining,
  tool talk, reply-all on a poll);
- mode agreement and proposed-time overlap with Claude;
- whether the event the user created by button was among each side's
  times;
- latency, steps, schema failures, cold starts, errors.

### Pass levels

- needs-reply: catch at least as many of the user's direct replies as
  Claude's reference labels do on the same messages (`nr_recall_outcome`
  against `nr_recall_reference`), and agree with those labels at an F1 of
  0.9 or more. Even Claude's labels catch only about 77% of direct replies:
  the rest are replies with nothing asked of the user (thanks, courtesy
  notes, small follow-ups), so a fixed 95% is out of reach for any model;
- deadline recall at or above 95%;
- zero invented times;
- graded scheduling replies "send as is" or "minor edit" at 90% or more,
  and no worse than Claude's;
- fewer than 2% mail errors and 5% scheduling errors;
- a fix for cold starts (the server swaps models; a cold call takes
  about 4.5 minutes) before any interactive use.

## Tuning (`preprocessor/mail_tuning.py`)

The first week showed the live extraction prompt missing a third of the
messages the user answered. The tuning harness searches for a better
setup before the clean measurement window starts.

**Decisions versus representation.** The remote model decides (needs a
reply, by when, what action, how urgent, how sensitive) and represents
(a free-text `brief` Claude would read instead of the mail). Decisions
are scored; the brief, the instructions, the thread context, worked
examples, the model, thinking and voting all evolve.

**Labels.**
- Outcomes, automatic: did the user reply within three days, and was that
  reply to this message (no one else wrote in between); ledger rows and
  their deadlines.
- Reference labels: Claude (Sonnet) answers the triage questions from the
  raw message and the four thread messages before it, judging each
  message as of its arrival. Batches never hold two messages of one
  thread, so a label cannot lean on what happened later.
- Mail labelled `keep_from_anthropic` never goes to Claude: no reference
  label, and never shown to the optimizer. It is scored on the outcome
  label alone (`nr_recall_outcome_kept`).

**Score.** Weighted: needs-reply F1 against the reference (0.35),
needs-reply recall against direct replies (0.30), action accuracy
(0.15), sensitivity F1 (0.10), reply-by date match (0.10), less a small
penalty for a brief over 700 characters. Pass levels stay as above.

**Splits.** dev: Oct 1 to 5 (stratified sample of 150), val: Oct 6 to the
freeze (100), test: mail received after the freeze. Each candidate is
judged with "today" set to the day the message arrived.

**Search.** Reflective evolution: each round, the two best candidates on
dev go to Claude (Opus) with their scores and up to twelve failures
(missed replies first); it writes two revised setups for each. Finalists
are scored once on val. `freeze --candidate ID` writes the private
`extract-prompt.json`; from then on the live tick uses that setup
(instructions, thread context fetched per message, worked examples) and
the reports cover mail received after the freeze only.

**Baseline frozen 2026-10-09 12:58 UTC: `c004`.** Four earlier thread
messages, no thinking, one worked example (a "no reply needed" message),
dev score 0.837: catches 95% of the user's direct replies against Claude's
77% on the same messages, 16 of 20 disputes with Claude settled in its
favour by what the user did. Reports from then on cover mail received
after the freeze. Known flaw to fix before the next round: the scored dev
sample takes every "needs a reply" message first, so the example pool
holds only negatives and a candidate asking for 3 or 6 examples got 1 or 3
negative ones. Reserve a balanced example set before sampling, then
re-score the leaders together.

```bash
python3 preprocessor/mail_tuning.py dataset      # cache text and thread (private)
python3 preprocessor/mail_tuning.py outcomes
python3 preprocessor/mail_tuning.py label --limit 500
python3 preprocessor/mail_tuning.py evolve --rounds 4 --children 2
python3 preprocessor/mail_tuning.py board --split val
python3 preprocessor/mail_tuning.py freeze --candidate c0NN
```

## Drafting A/B (`preprocessor/draft_ab.py`)

A live experiment, not a shadow: for each new Outlook draft whose body
came from the `email-author` subagent in a bot run, a blind coin flip
(`qwen_share`, default 0.5) decides who writes the body the user sees.

- **Same context.** The remote model gets what the subagent gets: its
  instructions (`.claude/agents/email-author.md`), the recipient's style
  profile and the voice guide (the two reads the subagent makes with its
  tools, done for it), the caller's prompt, and the text of any file that
  prompt names. Its body goes through the same em-dash fix and replaces
  Claude's body in the draft, quoted thread kept, before the Send card is
  posted. A remote body that is empty, asks for more (`NEED:`) or takes
  over 60 seconds is dropped and Claude's body stays (a fallback).
- **Paired by text.** A draft takes part only when the subagent's
  returned text is its body (`author_match`: 0.6 or more of the body's
  words, in order). A run can make a draft with `email_writer.py` and
  also call the subagent for another email, one whose contact has no
  address yet; that draft is the writer's and keeps its body (recorded as
  mode `writer` in the remote mode). The bot keeps the subagent's result
  whole in the run's tool log for this pairing.
- **Blind.** The card does not say who wrote the body; the assignment is
  in the private store.
- **The score is the user's edits.** The Send button reads the body
  being sent, and after the send records its word-level edit ratio
  against the body shown, whether it was sent unchanged (ratio at or
  below 0.02), and the revision rounds in between; Cancel records a
  rejection (a later send replaces it). Edits made in Outlook, through
  the Edit button or by asking for a revision all count.
- **The other version.** Claude's body is kept for a remote-arm draft and
  the remote body is generated in the background for a Claude-arm draft,
  so each sent draft also says which writer's first version was closer to
  what went out (secondary: the shown version anchors the edits).
- **Scorecard**, in the daily report and the final report: per writer,
  drafts, sent, unchanged, mean and median edit ratio, revision rounds,
  cancelled, fallbacks, and a 95% bootstrap interval for the difference
  in mean edit ratio (remote minus Claude).
- Not covered: drafts made by the cron lanes (the morning sweep), Gmail
  drafts, and drafts written without the subagent.

Switch: `"drafting": "ab"` in remote-llm.json, within `shadow_until`.

**Remote writer.** `"drafting": "remote"` ends the experiment and makes
the remote model the writer of every draft the A/B covered: no draw, no
end date. The subagent still runs first, so Claude's body is the
fallback when the remote one is empty, asks for more or is late (a cold
model swap takes longer than the 60-second limit). The send measure keeps
running; the report shows these drafts as `remote_writer`, apart from the
A/B's numbers. Revisions the user asks for are still written by the
Claude session, and the cron-lane and Gmail drafts stay with Claude.

### Drafting backtest (`preprocessor/draft_backtest.py`)

The A/B replayed on the past weeks' subagent drafts: the remote model
writes from the same prompt (style profile cut to mail sent before the
draft), and each pair is scored by edit ratio against what the user sent
(anchored on Claude, whose body he edited) and by a blind Sonnet judge in
both orders. Kept mail never reaches the judge.

`draft_review.py serve` puts the same pairs on a tailnet page for the
user's own blind vote: the task, the two bodies as Draft 1 and Draft 2
(order drawn per item, notes to the caller cut), a pick and an optional
"send as is / light edits / rewrite" per body. The writers, the sent
email and the judge's verdicts show after the vote. The server listens on
127.0.0.1 under a token path, adds one `tailscale serve` route (refused
while a Funnel is on) and removes it on exit; a request must carry the
node owner's `Tailscale-User-Login`. Votes go to
`draft-backtest/votes.jsonl`; `draft_review.py summary` and the backtest
report give the tally. The user recognises his own sent wording, which
often is Claude's, so the vote leans toward Claude.

## Digests on the remote model (`preprocessor/remote_digests.py`)

The two cron lanes that read mail in bulk, `list-digest` (every list in
`list-digests.json`) and the weekly
`admissions-digest`, run on the remote model instead of Claude when the
switch is on. The user approved the remote model for all mail, so no mail
these lanes read goes to Anthropic.

- **Same input, same post.** `list_digest.collect()` and
  `admissions_filter.collect()` read the mail as the preprocess step does.
  The remote model makes the judgment the skill asks of Claude: which
  items to post, their rank and their facts, and which students are worth
  a look. It gets the user's rules for the same scopes (per list entry
  `newsletters`, `newsletters:<list>` and the list's `scopes`; for
  admissions the scopes in the skill's header). Code writes the post in
  the skill's shape and posts it (see "Where it goes"). For list
  digests only,
  `list_digest.archive()` then moves the mail to Archive (`posted`, or
  `silent` with no post when nothing was worth posting).
- **Links.** The model picks links by index and code copies them from the
  item, so the model types no URL in the post. For a `prioritize` list
  with `follow_links`, a first call sets items aside by name, the page
  behind each other item is read owner-side under fetch_url's address
  policy (plain requests, no JavaScript, no PDFs; at most 20 per email,
  nearest deadline first, 2,500 characters each), and a second call ranks.
- **Failure.** An endpoint error, the open circuit breaker or a failed
  post posts nothing and archives nothing. The run exits non-zero and the
  mail waits for the next run. There is no fall-back to Claude.
- **Record.** `remote-llm/digests.jsonl` holds one line per run: counts,
  the disposition, the host and outcome of each fetch, and the post text.
  Stdout (the cron log) carries counts only.
- **Differences from the Claude lanes.** An email the parser cannot split
  is one item, so the post has at most one section for it. A page that
  needs JavaScript reads as empty. With chat on, the post is not copied
  to `~/.ea/state/reports/`. The admissions header uses a colon where the
  skill's template has an em-dash.
- **Where it goes.** Where the enforce-mode digest goes
  (`chat_notify.post_digest`): on Zulip the private `mail-digest` stream;
  on the web chat the digest channel, "Mail digests" (kind `digest`,
  made with the first digest); on Slack the `mail-digest` webhook when
  one is mapped, else `ea-assistant` as before. The Claude lanes' chat
  reader reads only the EA channel and never a channel of kind `digest`,
  so the post's text does not reach Claude. A digest never waits in the
  quiet-hours queue, which sits in the resident's space. The
  `*-digest-latest.json` files the Claude lanes left in `~/.ea/state/`
  are removed when the switch goes on.

Switch: `"digests": "remote"` in `remote-llm.json` (default `off`, no end
date). The crontab runs the module for both lanes:

```
30 15,18 * * *  python3 /opt/ea-system/preprocessor/remote_digests.py list-digest >> /home/<you>/.ea-private/logs/list-digest.log 2>&1
0 8 * * 1       python3 /opt/ea-system/preprocessor/remote_digests.py admissions-digest >> /home/<you>/.ea-private/logs/admissions-digest.log 2>&1
```

With the switch off, each verb execs `run-agent.sh <agent>` from the same
tree, and the Claude lane runs as before. `--dry-run` always takes the
remote path: it builds the post and prints counts, and it posts, archives
and writes nothing (the post is in the run record).

## Enforce (`preprocessor/mail_gate.py`)

The goal is that no mail reaches Claude in bulk. Claude still reads a
thread when a lane acts on it or the user asks in a room or thread.

- **The gate.** The preprocessor's detectors run on the raw inbox (a
  "thanks" the remote model files as FYI still proves a thread is not
  dropped). What they return goes through `mail_gate.Gate` before the
  ledger, the scheduling queue or any lane's JSON sees it: actionable
  mail (needs a reply, a task, a decision, a meeting time) carries the
  remote model's summary, ask, due date and offered times in place of
  the preview; FYI and awaiting-reply mail is left out; kept mail is
  left out. Subjects, senders and ids stay. Each mode's JSON carries a
  `mail_gate` block with the counts.
- **Held mail.** Mail the remote model has not read is read inline
  (four at a time, 60 to 120 seconds per run); what is still unread is
  held, out of the run's seen sets, so a later run surfaces it. Mail
  unread four hours after it arrived passes as sender, subject and ids
  (`remote_unread`), so an endpoint outage loses nothing.
- **The remote digest.** `mail_gate.py digest` (cron, three times a
  weekday and once a day at weekends) posts the left-out mail to the
  Zulip stream `mail-digest`, to the web chat's digest channel ("Mail
  digests", kind `digest`), or on Slack to the `mail-digest` webhook:
  kept mail first (flagged when it needs a reply), then FYI mail by
  domain. Code renders it from the remote model's records; no Claude
  model sees it, and the briefing's chat reader reads none of those
  places. With none of them (Slack without that webhook, or no chat) it
  posts nothing, says so and keeps its window for the next run; the EA
  channel is never the fall-back. Mail the list digest covers is not
  repeated.
- **Cron lanes' reads.** A `cron-` run reads no kept thread through the
  mediator: a read of one kept message or thread is refused before it
  runs, and search and list results lose kept rows
  (`mail_privacy.release_precheck` / `release_filter`). Bot runs (a
  room, a thread, the DM) read as before. An error while checking a cron
  lane's read refuses it.
- **The tagger** (`mail_tagger.py`) calls the remote model instead of
  Claude Haiku.
- **The writer.** With `"drafting": "remote"`, lanes get email bodies
  from `email_writer.py write` (a mediated verb that runs on its own
  mediator thread), the `email-author` subagent only on a fallback;
  `draft_ab.note_writer_draft` matches the draft to the writer's record
  so the send measure covers it. The brief is ea-bot's text, so each one
  sent is also recorded in the owner-private `outbound-owner.jsonl`
  (size, hash, first 200 characters, size of the whole request), and the
  daily outbound review posts a count and size line.

Not covered: Teams DMs in the briefing, the user's own sent mail where
it appears, ledger notes written before enforce, and plugins.

## Operating it

The switches are read and set with `remote_llm.py switches` and
`remote_llm.py set <switch> <value>` (docs/llm-lanes.md, section 1).

```bash
python3 /opt/ea-system/preprocessor/remote_llm.py status     # switches, key, store
python3 /opt/ea-system/preprocessor/remote_llm.py ping       # one tiny request
python3 /opt/ea-system/preprocessor/mail_privacy.py status
python3 /opt/ea-system/preprocessor/mail_privacy.py report   # numbers so far
python3 /opt/ea-system/preprocessor/sched_agent.py report
python3 /opt/ea-system/preprocessor/sched_agent.py run-text --force --text "..."  # one manual turn
python3 /opt/ea-system/preprocessor/remote_digests.py list-digest --dry-run       # counts only
```

To stop a part, set its switch (`remote_llm.py set mail off`, and so on;
docs/llm-lanes.md, section 6). To remove what was stored:
`mail_privacy.py purge`. A commit that changes mediation code restarts
the mediator as well as the bot.

## Sealed mail and the live scheduling agent

Two values added after enforce, both documented in `docs/llm-lanes.md`:

- `mail = sealed`: every lane's mail read, rooms and threads included,
  returns the remote model's record in place of the text
  (`mail_privacy.seal_output`, applied by the mediator's `release_filter`).
  A message it has not read is extracted inside the mediator within a
  budget. The thread brief (`mail_privacy.thread_brief`, written onto the
  ledger row by `sync_briefs` at the end of each tick) carries the same
  facts, so a Claude session can act from `ledger.py get` alone.
- `scheduling = enforce`: the scheduling channel and the proactive
  scheduler are answered by `sched_agent.run_live` before Claude would
  start; its writes run as the resident under the turn's run id, so the
  bot's Send and Create buttons follow as for a Claude turn. A turn that
  fails before writing anything falls back to Claude.

Each is gated by a ghost week (`ghost_week.py`): `mail = ghost` and
`scheduling = shadow --days 7` record what the cutover would have done,
a judge grades it daily, and the final verdict (GO, NO-GO, NOT ENOUGH
DATA) is what `remote_llm.py set` checks. `ghost_backtest.py` runs the
same judge on past exchanges from the EA's transcripts, grading the
ledger brief beside the record and, with `--replay`, re-running turns on
the sealed inputs for a true A/B.

## Not built

The calendar shim live, pseudonymization, Teams DMs through the gate,
Gmail and attachments (`scan-attachments` and `download-attachment` still
return text and files in every mode), and the live agent for scheduling
turns inside the EA channel, the DM and the project rooms, which stay on
Claude because those sessions hold the rest of the conversation.
