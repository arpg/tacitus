# Skill: Afternoon Catchup
<!-- scopes: briefing, mail-triage -->

> With `briefings` at `remote` in the owner-private remote-llm.json, this
> lane is written by the remote model (`preprocessor/remote_briefing.py`,
> docs/llm-lanes.md) and this skill is not run. It runs when the switch
> is off.

Slim afternoon check-in. Surfaces only what changed since morning,
plus a preview of tomorrow. This is NOT a full re-triage — it's a
quick status update.

## Design principle

The morning briefing is comprehensive. The afternoon catchup is fast.
Target: 5-10K tokens max, plus the reply-queue drafts (step 4.5).
Often 0 tokens if nothing changed.

## Steps

### 1. Load state (minimal)

- Read `~/.ea/state/briefing-latest.json` (focus on delta since AM)
- Check OPEN obligations: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status open` (updates since morning)
- Also load recently-handled items: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status completed`. Anything completed/closed (including work done since this morning) is DONE; the ledger wins over any inbox or DocuSign reminder.
- Do NOT re-read config files unless needed
- If the briefing JSON has a non-empty `calendar_fetch_errors` list,
  open the catchup with `:warning: Calendar source <account> failed to
  load — availability data is incomplete` and avoid presenting
  availability as authoritative.

### 2. Check what changed since morning

**New emails (Gmail MCP):**
- `is:unread newer_than:8h` — only things that arrived today
- Filter to: direct emails to the user (not CC, not mailing lists)
- For group threads: only flag if the user is directly addressed

**Calendar changes (Google Calendar MCP):**
- Check for new/modified events for tomorrow
- Check for cancellations of today's remaining events
- Check for same-day additions

**chat replies** (when a chat channel is configured; without one the
briefing JSON has no `recent_slack_messages`, so skip this block):
- Check `recent_slack_messages` in the briefing JSON
- `pending_instructions` = user messages with NO bot reply (truly pending)
- `handled_instructions` = user messages the bot already responded to (DONE)
- Only report `pending_instructions` as needing action
- Do NOT report handled instructions as "awaiting execution"

### 3. Determine what is ACTUALLY still pending

**The ledger is the single source of truth for item status.**

**Reconcile every candidate against the FULL ledger first (mandatory).**
Before surfacing or nudging on ANY item — including new emails from step 2,
calendar changes, and the step-4 nudges — cross-check it against both the
open AND the recently-completed ledger entries from step 1. The briefing
JSON's `recently_closed` list (last 7 days) is a hard DO-NOT-SURFACE list:
anything matching it is settled, no search needed. If it matches an
entry whose status is `completed`/`closed` (by conversation id, subject,
DA/award number, person, document name, or topic), it is ALREADY HANDLED:
drop it. A fresh reminder that lands after the underlying item was closed (a
re-sent or "Reminder:" DocuSign for a checklist you already signed, an NSF
report reminder, a re-issued invoice) is still that closed item, and a
DocuSign "Completed:" or "Voided:" notice supersedes any earlier "Complete
with Docusign:" reminder for the same document. When unsure, run
`ledger.py search --query "<entity/subject>"` before surfacing. Never nudge
the user to do something the ledger shows is done.

Use the LIVE `ledger.py list --status open` output from step 1.
Do NOT use the briefing JSON's `open_obligations` to determine what
is pending — it can be stale.

An item is pending ONLY if it appears in the live ledger as open AND
has no recent `last_action` showing it was handled today. For chat
instructions, only `pending_instructions` (no bot reply) are truly pending.

### 4. Proactive nudges (gather these for the update)

If any of these are true, include a nudge in the update:

- Drafted email in Outlook that hasn't been sent (>4 hours old):
  ":outbox_tray: You have an unsent draft re: [subject]. Still want to send?"
- Obligation marked "urgent" with no progress today:
  ":alarm_clock: [item] was flagged this morning — still open."
- Tomorrow has an early meeting (<9 AM) or travel:
  ":sunrise: Early start tomorrow: [event] at [time]"
- Friday afternoon: ":clipboard: Weekend preview: [Saturday/Sunday events]"

### 4.5. Work the reply queue

`reply_backlog` in the briefing JSON lists the threads that have waited
a day or more for the user's reply, oldest first. Take the 3 oldest
entries whose `has_draft` is false, whose `queued` is empty and whose
`kept` is false, and handle each one as the morning briefing does
(skills/morning-briefing.md, "Then work the reply queue"): draft the
reply with `create-reply --message-id <message_id> --conversation-id
<conversation_id> --ledger-id <id>`, or write one line under "Pending
your response" when a rail applies or only the user knows the answer.
Record each outcome with `ledger.py note-reply --id <id> --outcome
drafted|decision|left`. Skip this step when the list is empty.

### 5. Update state (do this BEFORE writing the update)

- Rows the user said are done: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py close --id <id> --note "reason" --user-said "<their words>"`.
  Rows that only look done: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py propose --id <id> --why "<evidence>"`;
  they stay open and go on the user's confirm card (CLAUDE.md rule 8).
- Do NOT reset delta tracking — that happens at weekly audit
- The catchup never asks a first-week question (ignore
  `onboarding_question` in the briefing JSON); it only records an answer
  the user gave in chat, with `onboarding.py answer --id <id> --text
  "<their words>"`, and confirms the rule in one line.

### 6. Skip if empty

If there's genuinely nothing new:
- Your text output should be: ":white_check_mark: All quiet since this morning. Tomorrow: [N events]. No action needed."
- Or skip posting entirely if the morning briefing covered everything
  and no new mail arrived.

### 6.5. If a PRE-SEND AUDIT RESULT block is in your prompt

Your previous draft was checked in code against the mailbox and the
ledger, and was not posted. The rows the block lists as CLOSED are
closed: drop them from "Pending your response" and name them in "Since
this morning" with the evidence in a few words. The rows it lists as
ambiguous stay, with the evidence added as one short clause ending in
"still open?". Change nothing else, and call no tool to re-check.

### 7. WRITE THE UPDATE (MUST be your FINAL action — no tool calls after)

Your text output IS the chat message. The orchestrator posts it directly.

**CRITICAL: `claude -p` only captures text from the final assistant turn.
Any text you write BEFORE a tool call is silently dropped. Therefore the
update text MUST be the absolute last thing you produce. Do ALL tool
calls (state updates, ledger, file writes) BEFORE this step.**

Your FIRST line of text MUST be `:coffee: *Afternoon Update —` followed by the date.
Do NOT write meta-commentary ("update posted", "state updated"). Write the
actual update the user will read.

Format (use mrkdwn: `*bold*` single asterisks, `:emoji:` shortcodes):

```
:coffee: *Afternoon Update — [day, date]*

*Since this morning:*
- [1-3 bullet points of what's new, or "Nothing new"]

*Remaining today:*
- [events for rest of today, if any]

*Tomorrow preview:*
- [tomorrow's events, one line each]
- [any deadlines due tomorrow]

*Pending your response:*
- [items from `pending_instructions` only — NOT handled_instructions]
- [open ledger items with no recent action — cross-check ledger status]

:envelope: *Drafts ready* ([N]): [one line for each draft this run made; leave the section out when it made none]

:inbox_tray: *Reply queue*: [N] replies owed, the oldest for [N] days. [N] have a draft ready for Send. No draft yet: [who or subject] ([N]d).
[ONE line from `reply_backlog`, as in the morning briefing; leave it out
when no entry has a draft and none is 3 days old or more.]
```

After writing the update text, STOP. Do NOT make any more tool calls.
The update is complete.
