# The LLM Harness: Running Claude Code as an Executive Assistant

How the EA turns a general-purpose Claude Code CLI into a persistent,
context-aware, and structurally-constrained executive assistant.

This document covers two things the rest of the docs only sketch:

1. **The resident session** -- one long-lived interactive Claude
   conversation that every chat message (Zulip or Slack) is dispatched
   into, and how it
   is spawned, addressed, and recycled.
2. **Context handling** -- how each dispatch carries only the context that
   turn actually needs, instead of re-sending the whole operating manual
   every message.

For the security threat model see [docs/SECURITY.md](SECURITY.md); for
the end-to-end message flow and prompt construction see
[docs/ARCHITECTURE.md](ARCHITECTURE.md); for the token-custody design
see [docs/mail-mediation-spec.md](mail-mediation-spec.md).

---

## 1. Why a harness at all

Claude Code is a general coding agent: a fresh process per invocation, full
tool access, no memory between runs. An executive assistant needs the
opposite of all three:

- **Continuity.** "Draft a reply to Oakley," then "make it warmer," then "ok
  send it" is one conversation, not three cold starts. Re-establishing who
  Oakley is and what the thread was about on every message is slow and
  lossy.
- **Selective context.** The operating protocol, house writing style, and
  per-skill instructions are large and static. Pasting them into every
  turn dilutes the actual ask and burns the transcript budget that
  triggers session rotation.
- **Structural constraint.** The assistant reads your mail and drafts in
  your voice, but it must be *unable* to send mail, mutate your calendar,
  or read the raw OAuth tokens, no matter what a malicious email tells it
  to do.

The harness is the machinery that supplies all three. Its two moving parts
are the **resident session** (continuity) and **`resident_context.py`**
(selective context), both running inside a **`ea-bot` Unix-user sandbox**
(structural constraint).

---

## 2. Two dispatch lanes

A chat message can be handled in one of two ways. The bot
(`ea-chat-bot.py`, runs as the owner) tries them in order:

| Lane | What it is | When used |
|---|---|---|
| **Resident pane** (primary) | One persistent interactive Claude session in a tmux pane, running as `ea-bot`. Every message is pasted into it. | Default. `resident_bridge.py` owns it. |
| **Per-thread session** (fallback) | A `claude --resume <uuid>` subprocess, one session id per Slack thread. | Only when the pane is down, unhealthy, or already busy with another thread. `slack_sessions.py` owns it. |

The bot reaches its chat platform through a front-end layer
(`preprocessor/chat/`). `FRONTEND` is the platform `chat.frontend` in
`ea-config.json` names: Zulip, the recommended self-hosted platform, or
Slack (an install with no `chat` block runs Slack). Each frontend turns its
platform's events into neutral `InboundMessage` and `ActionEvent`
values and dispatches them through one handler registry, which the bot
binds as `app`, so the lanes above never see which platform a message
came from. A thread key is an opaque string: a Slack ts, or a Zulip
`stream:<id>:topic:<topic>` key. It is the key `slack_sessions.py` and
the bridge's `[slack-dispatch thread=...]` marker already carry. The
dispatch text the resident receives and the prompt wording come from the
frontend's `prompt_strings()`; on Slack they are the sentences they have
always been. A confirmation is a card the frontend renders: Slack
buttons, or on Zulip a message with one reaction chip per action and a
one-time code, which the owner confirms with one tap or by replying
with the code. See
[docs/ARCHITECTURE.md](ARCHITECTURE.md) ("Chat front-end layer").

A third, unrelated lane exists for **scheduled agents** (morning briefing,
afternoon catch-up, weekly audit) via `run-agent.sh` on cron. Those are
one-shot, full-context invocations, not part of the conversational harness;
see [docs/ARCHITECTURE.md](ARCHITECTURE.md) §2.

The rest of this document is about the resident pane, because that is where
almost every interactive message lands.

---

## 3. The resident session

### 3.1 What it is

The resident is a single interactive Claude Code process that stays alive
across many Slack dispatches. It lives in a tmux session named
`ea-resident`, kept up by a respawn loop in `scripts/ea-resident-pane.sh`.
Its identity (a UUID `session_id`, a creation timestamp) is recorded in
`~/.ea/state/resident-pane.json`. Because it is one continuous
conversation, the model *remembers* earlier turns: the operating protocol,
who it just drafted to, the thread it is mid-way through.

### 3.2 How it is spawned

`scripts/ea-resident-inner.sh` runs inside the pane (as the owner) and `exec`s
Claude as `ea-bot`:

```bash
exec sudo -n -u ea-bot env \
    HOME=/home/ea-bot \
    EA_DIR=/home/ea-bot/.ea \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin:/home/ea-bot/.local/bin \
    bash -c '
        cd /home/ea-bot/ea-system || exit 1
        exec /home/ea-bot/.local/bin/claude \
            "$1" "$2" \
            --disallowed-tools "$0" \
            --dangerously-skip-permissions \
            --model "$3"
    ' "$DISALLOWED_CSV" "${SESSION_FLAGS[@]}" "${EA_RESIDENT_MODEL:-claude-opus-5-5[1m]}"
```

Key properties:

- **User: `ea-bot`.** The interactive session is NOT you (the owner account). It is the
  unprivileged service account whose home is `/home/ea-bot` and whose only
  writable state is the shared `~/.ea`. This is the sandbox boundary; see
  §6 and [docs/SECURITY.md](SECURITY.md) Layer 1.
- **Model: `claude-opus-5-5[1m]`** by default (override with
  `EA_RESIDENT_MODEL`). Interactive conversations run on Opus. The
  scheduled agents pick their own default in `run-agent.sh` — Opus 5.5,
  pinned by name, for the three synthesis agents (morning-briefing,
  afternoon-catchup, weekly-audit), sonnet for everything else — and the installed cron
  lines override the daily two back to `--model sonnet`.
- **`--disallowed-tools`** is built from `scripts/disallowed-tools.txt` (see
  §6). It denies the send / calendar-mutate verbs.
- **`--dangerously-skip-permissions`** is deliberate. The `ea-bot` Unix user
  *is* the sandbox, so there is no human at the keyboard to answer an
  interactive permission prompt -- and a prompt would freeze the whole
  dispatch lane. The deny-list and the code-level confirm-token gates still
  apply under this flag, so bypassing the interactive prompt does not
  weaken the external-action gates.
- **Session flags.** First boot uses `--session-id <uuid>`; every later boot
  uses `--resume <uuid>` (chosen by testing whether the transcript
  `~/.claude/projects/-home-ea-bot-ea-system/<uuid>.jsonl` exists). So the
  conversation survives pane restarts, not just message-to-message.

An `--allowedTools` list is also maintained in `ea-resident-inner.sh`
(mirroring the bot's MCP + Bash allowlist), but it is documentation /
pre-approval only: under `--dangerously-skip-permissions` the allowlist is
moot and the deny-list is what bites.

### 3.3 The dispatch protocol

The bot does not use stdin or an API. It **pastes text into the pane** via
tmux (`resident_bridge.dispatch_and_await` -> tmux `load-buffer` /
`paste-buffer` / `send-keys`, under a short flock so concurrent pastes
cannot interleave). Each dispatch is wrapped in a machine-readable marker:

```
[slack-dispatch thread=<thread_ts> id=<msg_ts>] <Name> says:
<the user's message>
```

The contract the model is taught (via the primer, §4.2):

- **Reply routing.** Whatever the model writes *between one
  `[slack-dispatch ...]` marker and the next* is the reply to that marker's
  Slack thread. The bot reads the transcript from a saved offset, isolates
  the segment for this dispatch id (`find_segment_reply`), waits for ~3s of
  quiescence so it does not post a half-written reply, strips em-dashes,
  and posts it back in <=3000-char chunks.
- **Async work.** For long tasks the model launches a background subagent
  and replies immediately that it is running. When the subagent finishes,
  the model emits a message beginning with the literal line
  `[deliver:<thread_ts>]`, which the router delivers to the right thread
  even though no new dispatch prompted it.
- **Late replies.** If a dispatch exceeds its wall-clock budget (~420s) the
  bot does *not* re-send it; the pending entry stays registered
  (`resident-pending.json`) and a router thread delivers the answer
  whenever it lands.

### 3.4 Concurrency

The pane is a single conversation, so only one dispatch can be "in the
foreground" at a time (`resident_bridge.claim_inflight`). A follow-up to a
thread the resident is already handling **queues** for the pane (preserving
continuity). A message for a *different, unseen* thread while the pane is
busy **spills** to the per-thread fallback lane so unrelated requests run in
parallel. `resident_context.thread_seen()` -- not Slack's `is_thread_reply`
-- decides which, because after a rotation a Slack "reply" is brand new to
the resident.

### 3.5 Rotation

A long conversation eventually needs recycling. `maybe_rotate()` runs before
each dispatch and rotates when the transcript passes ~500 KB (and no
dispatch is pending, since pending replies live in the current transcript);
a daily maintenance rotation also runs. Rotation kills the tmux session and
deletes `resident-pane.json`; the next health check mints a fresh
`session_id` and a new transcript. **Rotation is also what resets context
tracking** (§4.4), because the tracker is keyed by `session_id`.

---

## 4. Context handling

This is the heart of the harness: deciding, per dispatch, what context is
actually *new* to the resident. It all lives in
`preprocessor/resident_context.py`, whose `build_dispatch()` is a **pure
function** (no I/O beyond reading the tracker) so the selection logic is
unit-tested in `tests/test_resident_context.py`.

### 4.1 The problem it solves

Before this module, every dispatch re-injected the same boilerplate -- the
full operating protocol, the house style rules, and the detected skill
`.md` files -- even though the model had already seen them earlier in the
same conversation. That bloated each turn, diluted the ask, and accelerated
the size-triggered rotation. The fix splits context into two axes.

### 4.2 Axis 1 -- the durable primer (once per session)

`PRIMER` is the operating protocol plus the house `STYLE_RULES`
(no em-dashes, no LLM-tell adverbs, per-contact style matching). It teaches
the dispatch/reply contract, the "never send-draft, the bot posts the
`:lock:` buttons" rule, the calendar `pending-event.json` flow, the
`style_profiles.py` lookup step, and absolute-path conventions. It is
injected **once per resident session** and the model carries it for the
rest of the conversation.

A 2-line `GUARDRAIL` rides **every** dispatch as cheap insurance against
auto-compaction summarizing the primer away:

```
[Reminders: NEVER send-draft (bot handles Send buttons);
[deliver:<thread_ts>] only for async background reports;
Slack mrkdwn *single asterisks*, no em-dashes.]
```

Crucially, the primer and guardrail are *prompt-level reminders, not the
enforcement*. The dangerous verbs are denied by `--disallowed-tools` and
gated by code-level confirm tokens (§6). So a primer that gets compacted
away is **degraded quality, not a safety hole**.

### 4.3 Axis 2 -- per-dispatch targeted context

- **Briefing extract: ALWAYS injected when non-empty.** It is entity-matched
  to *this* message (obligations, threads, and people the message mentions)
  and is the freshest ground truth. It is never gated, precisely because a
  continuation about a new entity needs *that* entity's current data; this
  is what defeats the wrong-thread staleness bug.
- **Scheduling rules: gated per `(session, thread)`.** Injected the first
  time a thread needs them this session, then skipped.
- **Skill `.md` blob: gated per `(session, thread)` by the detected-skill
  set.** A new or expanded skill set re-injects the whole blob; an
  unchanged set relies on in-conversation memory.

### 4.4 The tracker

State lives in `~/.ea/state/resident-context.json`, keyed by the current
resident `session_id`:

```json
{ "session_id": "<uuid>", "primer_injected": true,
  "threads": { "<thread_ts>": ["scheduling", "skills:reply,schedule"] } }
```

`_load_state(session_id)` returns this **only if the stored `session_id`
matches**; a mismatch (rotation changed the id) or a missing/corrupt file
both yield fresh state. So rotation **auto-resets** context tracking with no
cron hook, and the file is a **cache, not a source of truth**: losing it
costs at most one redundant primer injection. `note_dispatched()` is the
only writer (atomic tmp+replace); `build_dispatch()` only reads.

### 4.5 What gets injected, when

| Segment | Fresh session, new thread | Continuation (thread seen this session) | After rotation |
|---|---|---|---|
| Primer + STYLE_RULES | yes | no | yes (reset) |
| Dispatch marker + message | yes | yes | yes |
| Briefing extract (if present) | yes | **yes** | yes |
| Scheduling rules | yes | no | yes (reset) |
| Skill blob | yes (if skill detected) | no (unless skill set expands) | yes (reset) |
| Guardrail | yes | yes | yes |

"New vs continuation" is the tracker's call, not Slack threading. A Slack
reply after a rotation is treated as new (full context) because the
resident genuinely has amnesia about it.

---

## 5. Draft detection

The resident creates Outlook/Gmail drafts by running
`graph_actions.py create-draft` / `create-reply` (SAFE verbs). The bot
learns what was drafted -- so it can post the `:lock:` Send/Cancel buttons
-- from a structured journal, not by scraping stdout. `draft_events.py`
appends one JSONL line per draft mutation (`created` / `updated` /
`deleted`, with the draft id). The bot snapshots the journal offset before a
dispatch and reads `effective_drafts(read_since(offset))` after, giving it
an exact created/updated/deleted set. (Gmail drafts made via the MCP tool,
which is outside ea-system's control, still fall back to a stdout scan.)

---

## 6. The safety envelope

The harness is what makes an untrusted-input agent safe to point at your
inbox. Four independent mechanisms, none of which the LLM can talk its way
around:

1. **`ea-bot` Unix-user sandbox.** The resident is not you. It cannot read
   `/home/<you>`, cannot write the read-only deployed source, and (see below)
   cannot read the OAuth tokens. Enforced by the OS.
2. **`--disallowed-tools` deny-list** (`scripts/disallowed-tools.txt`):
   `send-draft`, `send-gmail-draft`, `create-event`, `update-event`,
   `delete-event`, `schedule-send`, and `request-confirm` (the token
   minter), each in both `python3 *graph_actions.py ...` and bare
   `*graph_actions.py ...` forms. Also `gmail_client.py send-draft`,
   `scheduled_sends.py add`, the `python3 -c *<module>*` import routes
   around the CLI, every Gmail MCP send/delete verb, every Google Calendar
   MCP write verb, and the Slack MCP posting verbs. Applies even under
   `--dangerously-skip-permissions` — on this lane it is the ONLY thing
   that applies, so anything missing from that file is reachable here.
3. **Code-level confirm tokens.** Even if a verb were somehow invoked, the
   send / calendar-mutate paths in `graph_actions.py` require a one-time
   token that only the bot mints, and only after you click a Slack button.
   A deferred send needs a second credential on top: the queue entry is
   HMAC-signed with a key only the owner can read, and the `*/5` cron verifies
   it fail-closed before sending.
4. **Mail mediation (token custody).** The decisive layer. The Graph/Google
   tokens are owner-only `0600`, unreadable to `ea-bot`. The resident's
   SAFE verbs (read / draft / search) are transparently forwarded to a
   owner-side `ea-mail-mediator` service that holds the tokens and executes
   them; GATED verbs are refused there too. So a prompt-injected resident
   cannot send mail because it physically **cannot obtain a send-capable
   credential**, not merely because it is told not to. See
   [docs/SECURITY.md](SECURITY.md) and
   [docs/mail-mediation-spec.md](mail-mediation-spec.md).

The relationship between the harness and mediation is worth stating plainly:
**the `ea-bot` user that the resident runs as is exactly the user that
mediation locks out of the tokens.** The harness gives Claude continuity and
context; mediation guarantees that continuity and context can never turn
into an unapproved send.

---

## 7. State and file reference

| Path | Written by | Purpose |
|---|---|---|
| `~/.ea/state/resident-pane.json` | `ea-resident-pane.sh` | Current `session_id`, tmux session, created ts |
| `~/.ea/state/resident-context.json` | `resident_context.note_dispatched` | Per-session primer/skill/scheduling injection tracker |
| `~/.ea/state/resident-pending.json` | `resident_bridge` | In-flight dispatch registry (late-reply recovery) |
| `~/.ea/state/resident-router.json` | `resident_bridge` | Transcript read offset + already-posted ids |
| `~/.claude/projects/-home-ea-bot-ea-system/<uuid>.jsonl` | Claude | The resident transcript (as `ea-bot`) |
| `scripts/ea-resident-pane.sh` | -- | tmux respawn loop / `ensure` / `rotate` |
| `scripts/ea-resident-inner.sh` | -- | The spawn command (user, model, flags) |
| `scripts/disallowed-tools.txt` | -- | Shared send/mutate deny-list |
| `preprocessor/resident_bridge.py` | -- | Pane lifecycle, dispatch, reply extraction |
| `preprocessor/resident_context.py` | -- | Per-dispatch context selection |
| `preprocessor/draft_events.py` | -- | Structured draft-mutation journal |

---

## 8. Related documents

- [docs/ARCHITECTURE.md](ARCHITECTURE.md) -- full message flow, prompt
  construction, approval-gate lifecycle, preprocessing pipeline.
- [docs/SECURITY.md](SECURITY.md) -- threat model, autonomy tiers, the
  four defense layers, and what prompt injection cannot do.
- [docs/mail-mediation-spec.md](mail-mediation-spec.md) -- the
  token-custody design and its rollout.
- [CLAUDE.md](../CLAUDE.md) -- the operating instructions the resident follows
  (the primer is a distilled, session-scoped subset).
