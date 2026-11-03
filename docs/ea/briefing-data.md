## Data sources

When `mail_gate.mode` is `enforce`, no email text is in this file. An
email item's `body_preview` is the remote model's summary (with its ask,
due date and offered times; `remote_record` has the fields). FYI mail and
mail kept from Claude are left out; the remote digest covers them. Pull
the thread with `get-conversation` when you act on an item.

The preprocessor (Python, no Claude tokens) produces a DELTA briefing JSON:
- `today_top`: ranked "needs doing today" queue, scored in code (deadline
  proximity, VIP, staleness, CC-only demotion). Each entry carries a
  `reason`. Briefings lead with this; don't rebuild the ranking. A row
  whose `follow_up_after` or `snooze_until` is today scores as due today
  with the reason `window opens today`, plus `at <time>` when its
  description names a clock time: it was parked for this day.
- `everything_else`: actionable but not top-ranked — digest as one line.
- `handled_by_reply`: items the user already answered by email (matched
  against sent mail, in-thread or same person+subject). Never surface as
  open; recap in one line.
- `new_dropped_threads`: conversations with new inbound, no reply
- `new_needs_response`: new emails signaling a response is expected
- `new_scheduling_requests`: new emails asking about availability
- `scheduling_channel_active`: true when a dedicated scheduling channel
  runs those requests as its own conversations. Condense them to a
  one-line handoff instead of briefing them in full.
- `travel_reimbursement`: unreimbursed travel findings
- `availability_merged`: free slots across ALL three calendars (Outlook + both Google)
  Already merged by the preprocessor — no need to re-check.
- `calendar_fetch_errors`: non-empty when a calendar source failed to
  load. `availability_merged` is then INCOMPLETE: warn, do not schedule.
- `open_obligations`: the running ledger of tracked items. A row with
  `parked_until` is not actionable before that date (a future follow-up
  or snooze date). Do not raise it anywhere in the post, not even as a
  flex. The ranker keeps it out of `today_top`, and the pre-send audit
  strips it from a draft that names it.
- `recently_closed`: ledger items closed in the last 7 days.
  DO-NOT-SURFACE list. Never brief anything here as open or pending.
- `lapsed`: rows this run closed because their date passed and their
  mail asked nothing of the user (a vote, a talk, an RSVP). Name them in
  one "Quietly handled" line, never as missed. An open row whose mail
  asks nothing (`ledger.lapses`) is never a to-do or OVERDUE.
- `staged_drafts`: the Outlook Drafts folder, verbatim. This is the ONLY
  permissible source for a "click Send" instruction; ledger notes about
  drafts go stale the moment the user sends.
- `auto_merged`: sibling items folded into one group this run (shared
  ticket, manuscript or award id). Announce each in one line so a bad
  merge is caught.
- `todays_events` / `tomorrows_events`: Outlook calendar
- `working_hours`: from Outlook mailbox settings
- `deadline_items`, `pending_rsvps`, `noisy_group_threads`: extracted
  from the same inbox pass, no extra API calls
- `stale_obligations`, `resurrected_follow_ups`, `woken_snoozed`,
  `scheduled_sends_due_today`: automated bookkeeping. The last one is
  the day-of preview that gives the user a chance to cancel a queued send.
- `writing_style`: recent-window voice data (see Communication style)
- `onboarding_question`: the first-week question of the day
  (`preprocessor/onboarding.py next`, docs/ea/onboarding.md), or null.
  The morning briefing ends with it under `:seedling: *One question*`.
- `recent_slack_messages`: the last 24h of the EA channel, already
  fetched. Includes `pending_instructions` (user messages with no bot
  reply yet). Read this instead of a chat read.
- `recent_teams_dms`: Teams 1:1 DMs from the last 24h, read from the
  Teams chat service by the teams-dms owner plugin (for a tenant that
  blocks Graph Chat.Read). Empty when the plugin is not installed or
  holds no token.

### Thread context fields (on each email item)

Each item in `new_needs_response` and `new_dropped_threads` includes:
- `to`: list of To recipient addresses
- `cc`: list of CC recipient addresses
- `user_is_direct_recipient`: true if the user is in the To field
- `user_is_cc_only`: true if the user is only CC'd (not in To)
- `user_already_replied_in_thread`: true if the user sent a message
  in this conversation and new inbound arrived after

**Use these fields to avoid tone-deaf triage:**
- If `user_is_cc_only` is true, the email is likely informational.
  Do NOT suggest "want to send a quick thanks?" or draft a reply
  unless the body clearly asks the user to act. Read the `body_preview`
  and `to` field to understand who the email is actually addressing.
- If `user_already_replied_in_thread` is true, this is a follow-up
  message in a thread the user already participated in. It may be
  informational (someone else also replying). Only flag it if the new
  message directly asks the user a new question.
- When summarizing: describe the thread from the user's perspective,
  not the sender's. "Harper replied to Oakley's committee request
  (you're CC'd)" is correct. "Harper agreed to serve, want to
  thank him?" misreads the thread.

You also have direct access via MCP connectors to:
- Gmail (read, draft, send)

For Google Calendar, use the Python client directly (NOT MCP):
```bash
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py \
  get-events --email <email> --start <ISO> --end <ISO>
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py \
  create-event --email <email> --summary "..." --start <ISO> --end <ISO>
```
This works for BOTH Google accounts and auto-refreshes tokens. Do NOT
use gcal MCP tools — they are unreliable in subprocess environments.

The chat is Zulip (on a Slack install the same rules hold, and the Slack
read tool a lane may grant is for when you have no briefing). You have
no chat read tool on Zulip: `recent_slack_messages` in
the briefing (the name predates Zulip) carries the recent EA-stream
messages, and a dispatch carries its own thread. You hold no chat
credential either: the bot's Zulip API key lives in the owner's private
directory, which you cannot reach; do not look for a key in any env file. Posting still
works, and the path depends on how you were invoked:

- **Resident or room dispatch** (a chat message routed to you): write your
  reply as text. The bot relays it into the thread. Call no chat tool.
- **Cron agent or plugin run**: `run-agent.sh` and `run-plugin.sh` capture
  your FINAL text output and post it. Same rule: write the message, call
  no chat tool. Text you write BEFORE a tool call is dropped, so do every
  tool call first.
- **Posting text or a file into a THREAD**, from any invocation, including
  one where nothing captures your stdout: `chat_upload.py` (full command
  below). It stages the job for the bot to deliver. You have no path that
  posts to a stream outside a thread.

Use emoji shortcodes (`:sunny:`, `:email:`) not unicode, and
`*single asterisks*` for bold (Slack mrkdwn), not `**double**`; the bot
converts both for Zulip.

A file this turn wrote needs no upload: write its full path in the reply
and the bot attaches it after the turn (the rule is in CLAUDE.md, under
the chat channel). To attach any other file, such as one an earlier turn
made, or one from a run whose reply the bot does not post, shell out to
`chat_upload.py`:

```bash
python3 /home/ea-bot/ea-system/preprocessor/chat_upload.py \
  --channel <the stream in the thread key, e.g. stream:4> \
  --thread-ts <the thread key from your dispatch> \
  --file <path> [--file <path> ...] \
  [--comment "initial comment"]
```

The thread key is the `thread=` value of your `[slack-dispatch ...]`
marker (or the thread your prompt names), for example
`stream:4:topic:Event%20details%20for%20RSVPs`; the file lands in that
stream's topic. Swap `--file` for `--text "..."` to post a message with no
attachment into that thread.

You run as `ea-bot` and, by design, hold no chat credential, so this helper
does NOT post directly. It STAGES each job in `~/.ea/state/outbox/`, and the
bot, which holds the key, delivers it to the thread within a few seconds.
That is why `--thread-ts` is REQUIRED. The helper prints
`{"status": "queued", ...}` per job; the post itself happens bot-side.

For Outlook actions, shell out to `graph_actions.py`. NEVER import
`graph_client` directly (e.g. `python3 -c "import graph_client"`): under
mail mediation the Graph token cache belongs to the owner, unreadable to you,
so direct imports fail with PermissionError. Every supported operation is
a `graph_actions.py` verb, which forwards through the mediator. If a verb
you need is missing, say so instead of importing around it.
