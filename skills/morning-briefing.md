# Skill: Morning Briefing
<!-- scopes: briefing, mail-triage -->

> With `briefings` at `remote` in the owner-private remote-llm.json, this
> lane is written by the remote model (`preprocessor/remote_briefing.py`,
> docs/llm-lanes.md) and this skill is not run. It runs when the switch
> is off.

Run the morning executive briefing. Triage the inbox delta, handle
scheduling, take autonomous action where trusted, and post to chat.

## Steps

### 1. Load state
- Read `~/.ea/state/briefing-latest.json` (DELTA: only new items since last run)
  Three fields are precomputed in code — trust them over your own re-triage:
  - `today_top`: the ranked "what needs doing today" queue (each entry has a
    `reason`). The briefing LEADS with these, in order.
  - `everything_else`: everything actionable that didn't make the cut. Digest
    only — one line total, never itemized.
  - `handled_by_reply`: items the user already answered by email (matched
    against sent mail). One recap line, never surfaced as open asks.
- Check OPEN obligations: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status open`
- Check what was RECENTLY HANDLED so you never resurface it:
  `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status completed`. Anything
  completed or closed (including work the user did since the last
  briefing, e.g. overnight) is DONE. The ledger is authoritative over
  any reminder still sitting in the inbox. ALWAYS load this before
  deciding what to surface.
- Project context: `python3 /home/ea-bot/ea-system/preprocessor/project_store.py tree` for the
  live project map; `pack --project <id>` when an item needs its full
  situation. File notable new emails (`file-message`) and journal
  decisions/milestones as you triage - the store is the project memory.
  (active-projects.md is deprecated.)
- Read `~/.ea/config/scheduling-preferences.json`
- Read `~/.ea/config/autonomy-tiers.json`

### 2. Check the chat for user replies (from preprocessor)
This step applies when a chat channel is configured (the `assistant`
profile). Without one the briefing JSON has no `recent_slack_messages`
and there are no replies to check: skip to step 3.

The preprocessor fetches the last 24 hours of messages from the EA channel.
Check `recent_slack_messages` in the briefing JSON:
- `pending_instructions` = user messages with NO bot reply yet (act on these)
- `handled_instructions` = user messages the bot already responded to (skip)
- Only act on `pending_instructions`. Do NOT re-process handled instructions.

In pending instructions, look for:
- Approvals ("send", "yes", thumbs-up)
- Corrections ("change the time to...", "use this wording instead")
- Tier promotions ("just do scheduling for Dr. Smith from now on")
- Receipt uploads (files with trip context)
- Any other instructions

Each message has `thread_context` showing what the user was replying to.
Act on pending instructions first before processing new items.

### 3. Check Google Calendar (supplemental only)
The preprocessor already merges all three calendars in `availability_merged`.

**Calendar source failure check (do this FIRST):** if the briefing JSON
has a non-empty `calendar_fetch_errors` list, one or more calendar
accounts failed to load and `availability_merged` is INCOMPLETE — a dead
account looks identical to a free one. The briefing MUST open with:
`:warning: Calendar source <account> failed to load — availability below
may be wrong; do NOT treat unchecked time as free.` Do not propose
meeting times or present availability as authoritative until the source
is back.
Use Google Calendar MCP only if you need to:
- Check for very recent changes (event added in the last 30 min)
- Create a calendar event (hold, focus block, etc.)
- Look up details of a specific event

For standard scheduling proposals, use `availability_merged` directly.

### 4. Check Gmail
Use Gmail MCP to search for:
- Unread emails from the last 24 hours
- Any threads related to open obligations
- LoR requests, scheduling requests, receipt-like emails

### 5. Handle scheduling requests
Follow the workflow in `scheduling-rules.md` exactly.
Use `availability_merged` from the briefing JSON for availability data.

Act, do not describe. For each request: run `scheduling_engine.py
propose-meeting` and create the proposal reply with `create-reply`; when
a chat channel is configured its Send block is then waiting for the
user. When the sender named a time and `validate-draft` confirms it is
free, stage the invite instead (write `pending-event.json` with
`"online": true` for the video link) and draft the one-line confirmation
reply. The briefing then says what you staged, not what the user should
go and do. Without a chat channel nothing reads a staged file: do not
write `pending-event.json`; describe the proposed event in the briefing
and leave the draft in the Drafts folder for the user to send.

**If `scheduling_channel_active` is true in the briefing JSON:** a dedicated
#ai-scheduling-assistant channel is live and the EA opens each scheduling request
as a conversation THERE. Do NOT detail `new_scheduling_requests` in this briefing
or propose times here — condense them to a single handoff line:
":calendar: N scheduling request(s) — handling in #ai-scheduling-assistant."
(When the flag is absent/false, handle them here as usual.)

### 5a. Follow-ups, snoozed emails, and scheduled sends (pre-computed)
The preprocessor handles these automatically. Just report them:

- **`resurrected_follow_ups`**: Items whose follow_up_after date arrived.
  Already reopened in the ledger. Report: ":alarm_clock: Follow-up due: [description]"
- **`woken_snoozed`**: Snoozed emails moved back to inbox by the preprocessor.
  Report: ":email: Snoozed email returned: [description]"
- **`scheduled_sends_due_today`**: Drafts scheduled to send today.
  Report: ":alarm_clock: *Scheduled send today:* [description]"
  Show a brief preview. "Reply *cancel [keyword]* to stop, or it sends at [time]."
  To actually cancel one, run:
  `python3 /home/ea-bot/ea-system/preprocessor/scheduled_sends.py cancel --draft-id <id>`
  (the `draft_id` is on the `scheduled_sends_due_today` entry). That is the
  only `scheduled_sends.py` verb you may call: `add` is on the deny list,
  because it parks a send the `*/5` cron later performs. Cancelling removes
  the entry and leaves the draft in Outlook.
- **`stale_obligations`**: Open items older than 7 days (pre-filtered).
  Flag in the ":warning: Action items" section.

Do NOT call `wake-snoozed`, `due-today`, or manually scan the ledger for
follow-ups or stale items. The preprocessor did this already.

### 6. Handle items needing response

**Step 6a: Sanity-close before surfacing ANYTHING.** Before you write a
single line of the briefing, run a closeability pass on every item
across every briefing surface — `new_needs_response`,
`stale_obligations`, `deadline_items`, `resurrected_follow_ups`, etc.

**Ledger reconciliation comes FIRST and is mandatory.** Cross-check
every candidate item against the FULL ledger you loaded in Step 1, both
open AND recently-completed. The briefing JSON includes a
`recently_closed` list (last 7 days of closures) for exactly this:
treat it as a hard DO-NOT-SURFACE list. If a candidate item matches an
entry there (by id, conversation, person, or topic), it is settled —
drop it without searching, and never describe it as open, pending, or
"due" no matter what earlier briefings or emails said. If an item matches a ledger entry whose
status is `completed`/`closed` (match by conversation id, subject,
DA/award number, person, document name, or obvious topic), it is
ALREADY HANDLED: drop it silently from every surface. A fresh reminder
email that arrives after the underlying item was closed (an NSF report
reminder, a DocuSign "Final Grade Report" or "Complete with DocuSign"
reminder, a re-sent invoice) is still that same closed item, and the
ledger wins over the inbox. A DocuSign "Completed:" notice likewise
supersedes any earlier "Complete with DocuSign:" reminder for the same
document. When you cannot tell whether a surfaced item maps to an
existing entry, run `ledger.py search --query "<entity/subject>"`
before surfacing it. Never surface, draft, or flag something the ledger
shows is done.

The preprocessor's closure scrubber catches obvious cases (Outlook
reply detected, Gmail reply detected, regex-matched acknowledgment in
the latest inbound) but it misses semantic cases:

- The latest inbound is a soft acknowledgment that the regex doesn't
  catch (paraphrased "thanks", culturally specific closings, mixed
  reply that ends with "we're all set on this side").
- The user clearly acted on this item through a channel the scrubber
  can't see (chat reply to a colleague, in-person resolution
  referenced in a follow-up email, portal submission noted in a
  separate thread).
- A *related* email since the last briefing (different conversation
  but referencing the same submission / event / receipt) signals
  completion — e.g., a calendar invite confirms a meeting request, a
  shipping notification confirms a registration, a chat reply from
  the user states "the paper was submitted".

The window for "recent activity" is *since the last briefing*, not
just "after the ledger entry was created". For each item that fires
any signal, before referencing it in the briefing, propose the close:

```
python3 /home/ea-bot/ea-system/preprocessor/ledger.py propose \
  --id <ledger-id> --why "<the evidence, in a few words>"
```

The row stays open; when a chat channel is configured the bot puts it
on the user's confirm card, and one click closes it (CLAUDE.md rule 8).
Then take the item out of the queue and every other surface, and list it
on one "Looks done?" line near the end (with a chat channel: "confirm on
the card"; without one: "say so and it closes"), each with its evidence
in a few words, merged with the briefing's `close_proposals`. If the user told you in chat the
item is done, close it instead with `close --user-said "<their words>"`.
A row that repeats another open row closes with `close --duplicate-of
<id>`.

When in doubt (truly ambiguous — could be ack, could be a follow-up
ask), DO surface the item and let the user decide. The bar for a
proposal is "I would be embarrassed to draft a reply asking about this
when the thread clearly shows it's already handled."

**Step 6b: Category suppression.** The `## User rules` block appended
to this skill carries the user's standing suppression rules (kind `avoid`,
scopes `briefing` and `mail-triage`): sender and subject patterns that
must not surface, and the escalation tokens that override a suppression.
Apply them BEFORE step 6c and before any URGENT framing. A suppressed
item is dropped silently, and any related ledger entry is closed on
that rule: `ledger.py close --id <id> --user-rule "<the rule's text>"
--skip-archive`. When the user names a new category to
suppress, record it in that store (`preferences.py add --kind avoid
--scope briefing ...`); it is never written into this file.

**Step 6b2: The pre-send audit may send you back.** After your draft
validates, code checks every open item it presents as open against the
mailbox and the ledger (`open_claims_audit.py`), asks a tool-less second
call for a verdict on the ones with contrary evidence, closes the rows
judged done, and re-runs you ONCE with a `PRE-SEND AUDIT RESULT` block
at the end of your prompt. When that block is present, it is the last
word: the rows it lists as CLOSED are closed, so they leave the queue
and go to the Quietly handled line with their evidence in a few words;
the rows it lists as ambiguous stay, with the evidence added to their
line as one short clause ending in "still open?". Everything else in
your draft stays as it was. Do not re-litigate the verdicts, and do not
call any tool to check them: the check already ran.

**Step 6c: URGENT framing discipline.** Do NOT lead the briefing with
a `:rotating_light:` or `URGENT` block unless it contains items that
genuinely warrant it. The bar is:

- A hard external deadline within the next 48 hours (e.g., NSF Annual
  Report past due, manuscript submission deadline tomorrow, RSVP for
  an event today), OR
- A scheduled action the user committed to that's about to lapse
  (e.g., a calendar invite the user owes a reply on for a meeting in
  the next 24h), OR
- Sender-escalation tone on a compliance item that survived step 6b.

Stale compliance reminders, "FYI" emails, and items that were
mentioned in yesterday's briefing without new movement are NOT
URGENT. Reports that aren't yet overdue are NOT URGENT — list them
in the regular `:warning: Action items` section with their deadline
date, but no rotating-light header. The standing rule: if a report's deadline
is still in the future, flag it as "upcoming"; only escalate to
URGENT on the day it goes overdue.

**Step 6c2: Draft claims and note age — the anti-rot rules.**
`staged_drafts` in the briefing JSON is the Drafts folder, verbatim,
and it is the ONLY permissible source for any "click Send" /
"staged and awaiting" instruction. Ledger notes describing drafts
("two drafts staged awaiting Send") are narrative frozen at write
time — the user may have sent them minutes later. If a note claims a
staged draft that has no match in `staged_drafts`, the draft was sent
or deleted: say nothing, or say it went out. The same discipline
applies to all narrative claims: a note's assertion ("nothing written
yet", "still waiting on X") is only as current as the item's
updated_at — older than ~3 days, verify from live data (sent mail,
the folder, the thread) before repeating it as present-tense fact,
and never compute "N days sitting unsent" from a note's age.
(2026-08-20: the briefing's top two actions were Send-clicks on
drafts sent days earlier — every consumer of that briefing lost trust
at line one.)

**Step 6d: The queue is precomputed — narrate it, don't rebuild it.**
`today_top` already ranks the day (deadline proximity, VIP, staleness,
CC-only demotion, chat activity). Your judgment applies ON TOP of it:
you may drop a today_top entry that steps 6a/6b just closed or
suppressed, and you may promote something the scorer cannot see (a
semantic urgency in a body_preview), saying why. What you may NOT do is
flatten the ranking back into an undifferentiated list. Items carrying
`mentioned_in_slack` are already being discussed with the user — refer
to that thread ("in play in chat") instead of re-raising them cold.

**Step 6e: A window that opens today is today's first item.** A
`today_top` reason that starts `window opens today` (optionally `at
10:00am`) marks a row parked for this very day: tickets on sale, a
registration opening, a portal closing. It goes FIRST in "Get done
today", with the clock time, however the rest ranks. Then make sure the
alert exists: run
`python3 /home/ea-bot/ea-system/preprocessor/alert.py list`; if the row
has none, schedule one for 15 minutes before the moment:
`python3 /home/ea-bot/ea-system/preprocessor/alert.py schedule --id <ledger id> --at <YYYY-MM-DDTHH:MM>`.
The alert is a direct message to the user, delivered by the chat
server; the briefing post is not a notification. Never clear such a
row's deadline or park it past its date: the moment IS the deadline.

**For items that survive 6a:**

For each item in `new_needs_response` (ranked by response_signals):

**First, check thread context before acting:**
- If `user_is_cc_only` is true: this is likely informational. Summarize
  it as FYI, do NOT draft a reply unless the body clearly asks the user
  to act. Read the `to` field to understand who the email is addressing.
- If `user_already_replied_in_thread` is true: this is a follow-up in a
  thread the user already participated in. Only flag if the new message
  asks the user a new question. "Someone else also replied" is not
  actionable — mention it as informational.
- Read `body_preview` carefully to determine WHO the email is addressing,
  not just who sent it. "Dear Oakley" means it's to Oakley, even if the
  user is CC'd.

**Then, for items that genuinely need a response, do the work:**

Draft every reply you can write from the thread, the ledger and the
project store: the sender asked something answerable, a form or link is
requested, a meeting is being arranged, an intro or a yes is owed. This
is the routine the user otherwise does by hand every morning. Creating
the draft IS the ask under the ask-first tier: when a chat channel is
configured the bot posts the Send block with Edit and Handled buttons,
and one Send-all card when several are ready; without one the draft
waits in the Drafts folder and the user sends it from Outlook. Never ask
in the briefing whether you should draft.

1. Read the thread (`get-conversation`) when the preview is not enough.
2. Get the body from the remote writer (`email_writer.py write`, see
   CLAUDE.md "Drafting in the user's voice"; the `email-author`
   subagent on a fallback) with the recipient, the thread and what the
   reply must say, and create the draft with `create-reply --ledger-id
   <id>` (reply-all; `--sender-only` for polls). The id names the row the
   reply answers, so its Send closes that row; a task or deadline on
   the same thread goes on the confirm card instead of closing.
3. Record it: `ledger.py update --id <id> --last-action "<date>: reply
   drafted, awaiting Send"`.

Do not draft, and surface as a one-line question instead, when a rail
applies: money, contracts or legal commitments; program officers or
deans; declining an invitation or request; a first email to someone
outside the institution's domains; or a decision only the user can make. Say what
the decision is, not that a decision exists.

Note: items are surfaced once. After that, the obligations ledger tracks them.
If items were missed, run the preprocessor with `--rerun` to re-surface all.

### 7. Handle routine tasks autonomously
Check autonomy tiers and act accordingly:

**LoR uploads** (do-and-tell):
- If an LoR request came in and a matching letter exists, upload it
- Post: "📄 Uploaded LoR for [student] to [portal]. Here's what I sent: [preview]"

**Calendar holds** (do-and-tell):
- If there's a meeting prep needed, create a 15-min hold before it
- Post: "📅 Created prep block before your 2pm with [person]"

**Reimbursement** (ask-first):
- If receipts are accumulating for a trip, compile and show progress
- Post: "💰 Portland trip reimbursement: 5 receipts compiled. Missing: [items]. 
  Reply **prep** when ready to draft the submission."

### 8. Group thread digests (from preprocessor)

If `noisy_group_threads` in the briefing JSON has entries:
- For each group with ≥5 new messages, include a 1-2 line digest
  in the briefing: "📋 [Neighborhood list]: 8 messages re: event planning.
  No action needed from you." or "📋 [Board committee]: budget vote request:
  **your vote needed by Thursday.**"
- If there's a mute in `~/.ea/state/muted-groups.json` for this group,
  skip it silently.
- Offer "say **digest [group]** for full summary" if the thread is complex.

### 8a. Deadline alerts (from preprocessor)

If `deadline_items` in the briefing JSON has entries:
- Any deadline within 3 days: prominent alert at TOP of briefing
  "⏰ DUE SOON: [item] — [date]"
- Any overdue items: red flag
  "🔴 OVERDUE: [item] — was due [date]"
- Otherwise, include a "📆 Upcoming: [N] deadlines this month" line

**CRITICAL: Only report deadlines/overdue items from OPEN obligations.**
If an item's status is `completed` or `closed`, it is DONE — do NOT
report it as overdue regardless of its deadline field. The `list --status open`
output is the sole source of truth for active items.

OVERDUE names a date the user still owes something by. A meeting's date
is when it happens: once held, the row is done or stale, never overdue.
A reminder the user wrote back on after its date is history. `today_top`
marks both as "its [date] date passed; ..." with no OVERDUE; follow it.

### 8b. Pending RSVPs (from preprocessor)

If `pending_rsvps` in the briefing JSON has entries:
- Surface up to 3 most urgent RSVPs in the briefing:
  "📬 [N] pending RSVPs — say **rsvps** for the full list"
- For events within 48 hours: "🔴 RSVP NOW: [event] is [tomorrow/today]"
### 9. Update state (do this BEFORE the briefing text)
- Add new items to ledger: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py add --id <id> --type <type> --description "..." ...` (types: `ledger.py types`)
  Use rich `--notes` for qualitative context (e.g., "said he'd think about
  it at the group meeting"). The ledger is the ONLY persistent state between runs.
- Rows that look done: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py propose --id <id> --why "<evidence>"`
  (CLAUDE.md rule 8); close outright only on the user's word (`close --user-said`).
- Update `autonomy-tiers.json` if user promoted anything in chat
- Update `scheduling-preferences.json` if user marked someone as VIP

### 9a. First week: one question a day

When the briefing JSON carries a non-null `onboarding_question` (the
preprocessor ran `onboarding.py next`; it is null once the bank is done,
the user said stop, or a question asked today is still open), end the
briefing with it, after the last line of the template, in one short
paragraph under this header:

```
:seedling: *One question* ([remaining] left)
[question] [why] Example: [example]. Reply in this thread in your own
words; say *skip* to pass or *stop asking* to end the questions.
```

Never reword the question and never add a second one. When the user
answers in this thread, that turn records it with `onboarding.py answer
--id <id> --text "<their words>"` and confirms the rule in one line;
*skip* runs `onboarding.py skip --id <id>` and *stop asking* runs
`onboarding.py stop` (docs/ea/onboarding.md). The answer is a rule in
`preferences.json` from then on, and no briefing asks it again.

### 10. WRITE THE BRIEFING (MUST be your FINAL action — no tool calls after)

THIS IS THE MOST IMPORTANT STEP. Your text output IS the chat message.
The orchestrator captures your text and posts it to the user's channel.
There is NO other posting mechanism.

**CRITICAL: `claude -p` only captures text from the final assistant turn.
Any text you write BEFORE a tool call is silently dropped. Therefore the
briefing text MUST be the absolute last thing you produce. Do ALL tool
calls (state updates, ledger, file writes) BEFORE this step.**

**Your very first line of text output MUST be:**
`:sunny: *Morning Briefing —` (followed by today's date)

If your output does not start with the briefing content, the user sees
nothing. Do NOT summarize what you did. Do NOT say "state updated" or
"briefing posted" or "here's a summary". Write the ACTUAL briefing the
user will read.

CRITICAL — Message structure and formatting:
- Post ONE single message with the full briefing. Do NOT split across multiple messages.
- Use threads only for individual actionable items (draft previews, scheduling proposals)
  that the user will respond to separately.
- Use shortcode emoji (`:sunny:`, `:email:`) NOT unicode emoji characters.
  Unicode emoji may render as garbage via the API.
- The chat uses mrkdwn: `*bold*` (single asterisk), `_italic_` (underscore).

Morning briefing tone: LIGHTWEIGHT. The user's framing (2026-08-15):
"Here's what you really should get done today, ideally in this order
based on when you have time — but here are your flexes." The briefing
is an ordered do-list fitted to the user's actual gaps, then the slack, then
one-line bookkeeping. It is NOT a status report; every line either
tells the user what to do or can be deleted.

**Main briefing template** (used in both modes above):
```
:sunny: *Morning Briefing — [date]*

:dart: *Get done today* (in this order, fitted to your gaps)
1. [item] — [WHEN: "before your 9:30", "in the 1-2:30 gap"] — [why now]
2. [item] — [when] — [why]
[today_top sequenced into the free windows you compute from
todays_events + availability_merged. Max 5. Deadline items first. If
the day genuinely can't fit an urgent item, say which meeting could
give way. If today_top is empty: "Nothing is pressing today." and one
line on what's next on the radar.]

:leaves: *Flexes* (fine today, fine tomorrow)
- [max 4 one-liners: the rest of today_top plus anything from
  everything_else you'd promote; everything else stays unsaid]

:clipboard: [ONE compressed calendar line: "9:30 X · 11 Y · 2 Z"]

:white_check_mark: *Quietly handled*: [one line: autonomous actions,
handled_by_reply recap, step-6a closures count, drafts staged]
:link: [ONLY if `auto_merged` is non-empty — one line per group:
"auto-merged N emails about ENTITY under 'primary desc' — say
*unmerge <id>* if wrong". Merges must be visible; a silent bad merge
is the failure mode the user accepted this feature against.]

:envelope: *Drafts ready* ([N]) — [with a chat channel: "Send, Edit or Handled on each block in your DM; one Send-all card when several are ready"; without one: "in your Outlook Drafts folder"]
- [recipient/subject] — [what the reply says, in one line]
- [recipient/subject] — [what the reply says]
[Every draft created during this run, one line each. With a chat channel
the Send blocks are posted by the bot and this list is the index; without
one this list is the only notice.]

:question: *Needs you* ([N])
- [item] — [the decision in one line, and what you will do once it is made]

Open obligations: [N] | Inbox: [N] messages
```

`Open obligations: [N]` is `delta_summary.open_obligations`. When
`delta_summary.obligations_by_list` is present (the install turned the
split on), print its counts in place of it: `Open: [N] · Follow-up: [N] ·
Waiting on others: [N]`, with `· Snoozed: [N]` after Follow-up when it is
not 0. Never count the rows yourself. Waiting rows are owed by someone
else, and follow-up and snoozed rows are parked until a date, so none of
them is open work.

Then post threads ONLY for items requiring user input (draft approvals, scheduling choices).

After writing the briefing text, STOP. Do NOT make any more tool calls.
The briefing is complete.
