## Scheduled sends

To send an email at a future date (e.g. follow-up after someone's trip),
create the draft as usual, then STAGE the request: write
`~/.ea/state/pending-schedule-send.json` with
```json
{"draft_id": "<id>", "send_after": "2026-04-21T09:00",
 "description": "Eli follow-up", "ledger_id": "<optional>",
 "thread_ts": "<this thread>"}
```
A JSON array stages several at once. The bot posts a `:lock:`
Schedule/Cancel block; the click is what queues the send. Do NOT call
`graph_actions.py schedule-send` or `scheduled_sends.py add` — both are
denied, and the button is the only thing that mints the token.

Never pick the time yourself: if the user did not give one, ask.

The draft stays in Outlook Drafts. The morning briefing previews it the
day it is due. A cron then sends it at the scheduled time; the ledger
entry is marked completed and the send is noted silently in the next
briefing. To cancel a queued send:
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduled_sends.py cancel --draft-id <id>
```

## Snoozing emails

**Snooze duration is ALWAYS user-specified.** Never pick an arbitrary
duration (e.g., "next Monday"). If the user says "snooze this" without
specifying when, ask: "How long should I snooze this? (e.g., 3 days,
next Monday, April 15)". Snooze, close, and reply actions are driven
by natural language in chat (e.g., "that's done", "snooze the Eli
thread until Wednesday", "reply to Ari").

To snooze an email until a future date (removes from inbox now, brings
it back later):
```bash
python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py snooze-message \
  --message-id <id> --until "2026-04-21" --description "Eli follow-up"
```
This archives the message and records a snooze in
`~/.ea/state/snoozed-messages.json`. The morning briefing calls
`wake-snoozed` to move due items back to inbox. Also update the ledger
entry: set status to `"snoozed"` with `snooze_until` date. On that date
the preprocessor sets the row back to `open`.

## Follow-up items

When the user says "follow up later" or "remind me about this in April":
1. Snooze the email (`snooze-message`) to declutter inbox
2. Set ledger status to `"follow_up"` with `follow_up_after` date
3. If there is a draft to send later, stage `pending-schedule-send.json`
   (see "Scheduled sends") and let the user click Schedule
4. The morning briefing resurfaces items when `follow_up_after <= today`

## Timed alerts and page watches

A moment with a clock time (tickets on sale at 10:00, a registration
that opens at noon, a portal that closes at 5 pm) gets a direct message
to the user before it, scheduled on the chat server so it fires whether
or not the bot is up:

```bash
python3 /home/ea-bot/ea-system/preprocessor/alert.py schedule --id <ledger id> --at 2026-10-06T10:00 [--lead 15]
python3 /home/ea-bot/ea-system/preprocessor/alert.py list
python3 /home/ea-bot/ea-system/preprocessor/alert.py cancel --id <ledger id>
```

`--at` is local time. The alert is recorded on the row (`alert_at`,
`alert_scheduled_id`), so `schedule` is idempotent; `--force` replaces
one. A lane without the owner's chat credential stages the request and
the owner's cron flushes it within five minutes, so the verb works from
every lane. Set the row's deadline to the moment's date as well; the
morning briefing puts a row whose window opens today first.

A page that says "check back" gets a watch instead of a person:

```bash
python3 /home/ea-bot/ea-system/preprocessor/page_watch.py add --id <watch id> --url <URL> \
    --absent "<sentence that must disappear>" --message "<what to tell the user>" --ledger-id <ledger id>
python3 /home/ea-bot/ea-system/preprocessor/page_watch.py list
python3 /home/ea-bot/ea-system/preprocessor/page_watch.py remove --id <watch id>
```

`--present` watches for a sentence to appear instead. The owner's cron
runs `check` every 15 minutes; when the condition holds the user gets a
direct message and a personal-channel post, and the linked ledger row
is annotated. A watch fires once per change and is quiet until the page
changes back.
