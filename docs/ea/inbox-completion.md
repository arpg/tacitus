## Inbox as completion signal

The inbox is an alternate completion method. When the user archives an
email directly in Outlook (desktop, mobile, web), the detector row for
that mail ("needs a reply") is closed by `ledger_sync.py` (runs every
15 min via cron). This is silent: the user already knows (they archived
it themselves). Each row is judged by its own mail, and a move the EA
made itself (a snooze, a close's thread archive, the admissions filter,
the list digest) never counts: graph_client records every EA move
(`ea_moves.py`). A row the EA tracks (a task, a deadline) is not closed
by an archive; the user's word, a draft or event that names it, or a
confirmed proposal closes it (`preprocessor/closure.py`).

This behavior is configurable via `inbox_as_completion` in
`~/.ea/config/autonomy-tiers.json`. When `enabled: false`, ledger
items stay open regardless of inbox state and must be closed explicitly
through the EA. If the user says "don't auto-close from inbox" or
similar, set `enabled: false`.

Items with status `snoozed` are also checked — if the user manually
moves a snoozed email back and then archives it, the item closes.

## Pre-send open-claim audit and item feedback

Two layers guard what a briefing asserts as open (set 2026-09-02, after
a morning queue whose top three items were already done):

1. **Pre-send audit** (`preprocessor/open_claims_audit.py`, wired in
   `run-agent.sh` for morning-briefing, afternoon-catchup and
   weekly-audit). After the draft validates and before it posts, code
   finds every open ledger row the draft presents as open and gathers
   contrary evidence (the user's sent mail with that person on that
   topic, a decline of that invite, closed rows carrying the same
   manuscript or proposal id; mail in the row's own thread; archived
   mail from the person a waiting row waits on; rows closed since with
   the same thread or correspondent, with their notes). Rows WITH
   evidence, and in the remote briefing every row the post shows, go to
   a short, tool-less second Claude call: open, done or unsure, per item. Rows
   judged done get a close proposal (they stay open until the user
   confirms on the card), are logged to the missed-closure backlog with
   source `pre-send-audit`, and the agent rewrites its draft once with a
   `PRE-SEND AUDIT RESULT` note appended to its prompt. If you receive
   that note, obey it: take the named rows out of the queue and list
   them on the "Looks done" line; add the evidence clause to the
   ambiguous ones. Every run is logged to
   `~/.ea/logs/open-claims-audit.jsonl`.
2. **Item feedback** (`preprocessor/item_feedback.py`, the dashboard's
   per-item buttons and `/api/op` `disposition`). The user can say
   `done`, `not_mine`, `snooze` (until a date) or `still_open` about any
   item, with a note. `done` closes and logs a missed closure; `not_mine`
   closes without archiving and writes a `preferences.py calibrate`
   line under `briefing`; `snooze` parks the row and the email;
   `still_open` stamps `confirmed_open_at`, and the audit stops asking
   about that row for a week. Log: `~/.ea/logs/item-feedback.jsonl`.
   The CLI form, for a chat lane: `python3
   /home/ea-bot/ea-system/preprocessor/item_feedback.py --id <id> --kind
   done|not_mine|snooze|still_open [--note "..."] [--until YYYY-MM-DD]
   --source chat`.

## The nightly reconcile

`preprocessor/ledger_reconcile.py` runs at 01:30 as the owner. It
judges every open row against the remote model's thread records, one
Claude call per project, and never sees message text. What it writes:

- A close proposal (`close_proposal`, signals `llm:reconcile` and
  `llm:reconcile-duplicate`): the row looks done, or repeats another
  row. It stays open until the user clicks the card. Treat it as any
  proposal: never report it as closed.
- A dated line on `last_action` ending in `(reconcile)`: what the mail
  shows since the previous action.
- `conversation_id` on a row that had none, when its own reply cannot
  close it; on a reply row the thread is in the row's `reconcile`
  metadata (`thread`). Read it before you search for the thread.
- A project link and a filed thread, when the row's words name one
  project clearly.

If a reconcile note or link is wrong, say so in chat: the row's
`reconcile` metadata names the group and the night.

