# Skill: Weekly Deep Audit
<!-- scopes: briefing -->

Comprehensive weekly review. This runs on Opus for deeper reasoning.
Identifies patterns, approaching deadlines, and systemic issues.

## Steps

**CALENDAR GROUNDING (required).** Before you claim in the report that an event
is "not on the calendar," that a meeting is unscheduled, or that an invite is
"likely unaccepted," you MUST query the real calendar for that date. The
briefing only carries today/tomorrow events; it proves nothing about a date days
out. Check BOTH calendars (Outlook + Google):
```bash
python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py calendar-view --start 2026-07-31 --end 2026-08-01
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py get-events --email <gmail address from user-profile.json calendar_accounts> --start 2026-07-31 --end 2026-08-01
```
The Outlook `my_response` field (accepted / tentativelyAccepted / declined /
none / organizer) tells you whether an invite was accepted. NEVER assert "no
event found" or "invite unaccepted" unless `calendar-view` returns nothing for
that date. A false "not on your calendar" is a hard audit failure: an event
can sit on the calendar the whole time while the briefing never shows it.

### 1. Load full state
- Read `~/.ea/state/briefing-latest.json` (14-day full weekly briefing)
- Active obligations: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status open,snoozed`
- Recently-closed (last 7 days, for the "Completed this week" report section):
  `python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status closed,completed --stale 0 --limit 50`
  then filter client-side to `closed_at >= (today - 7 days)`.
- Full ledger dump is available via `ledger.py dump` for cross-referencing, but
  NEVER treat entries from the full dump as actionable without checking their
  `status` field first. See "CRITICAL: status filtering" below.
- Read `python3 /home/ea-bot/ea-system/preprocessor/project_store.py list --status active` and
  `tree` (the project store replaced active-projects.md)

**CRITICAL: status filtering.** Every ledger entry has a `status` field with
values `open`, `snoozed`, `closed`, or `completed`. Only `open` and `snoozed`
items are pending action. An item whose status is `closed` or `completed` has
already been resolved — its `last_action` and `closed_at` fields explain when
and why. When reporting pending work, you MUST filter to
`status in (open, snoozed)`. Do not derive "still pending" from
`date_identified` or staleness alone — those tell you when the item was found,
not whether it was resolved. Reporting a completed item as open is a hard
audit failure.

Note: Unlike daily runs, the weekly audit gets the FULL analysis (not delta).
This is the one run per week that does comprehensive pattern analysis.
The weekly preprocessor also resets delta tracking, so Monday morning
will be a fresh "first run."

### 2. Deep thread analysis
For the top 10 dropped threads by staleness, read each whole thread:
```
python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py get-conversation \
  --conversation-id <id> --max-chars 1200
```
One call per thread, oldest-first, quoted history stripped. Ten threads
cost ten calls, not one per message.
Read each carefully. Determine:
- Is a response actually needed, or is this informational?
- What's the consequence of continued non-response?
- What would a good response look like?

### 3. Grant & project deadline review
Cross-reference the project store (`project_store.py list`, per-project
`pack`) against:
- Emails from program officers or sponsored research office
- Calendar events related to project milestones
- Any NCE or reporting deadlines within the next 60 days

Flag any project where:
- A report is due within 30 days and no draft exists
- An NCE window is closing
- A program officer has emailed without response
- Budget period is ending with unspent funds

### 4. Travel reimbursement audit
Active reimbursement items only. Query explicitly:
```
python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status open,snoozed --type reimbursement
```
Then filter for items whose description/notes mention reimbursement/receipt/trip.
If the result set is empty, the Reimbursement section of the report MUST say
"No open reimbursement items" — do NOT invent one from older closed entries,
from the briefing's `travel_reimbursement` block (which may include already-
resolved findings), or from calendar context.

For each genuinely-open item:
- Identify the specific trip(s) from calendar data
- List all related receipts found (from the receipt_message_ids)
- Determine what's missing for a complete reimbursement package
- Draft the reimbursement submission email if possible

For items that show up as `status=completed` or `status=closed`: read their
`last_action` and `closed_at` fields. If closed within the last 7 days, they
go in "Completed this week." Otherwise omit them entirely — they are done.

### 5. Pattern analysis
Look across the full week's context log for patterns:
- Are certain types of emails consistently being dropped?
- Are there recurring scheduling conflicts?
- Are there senders who always get delayed responses?
- Any workflow improvements to suggest?

### 6. Obligations ledger maintenance
- Review all open items: are any actually resolved? Propose the ones that
  look done (`ledger.py propose --id <id> --why "<evidence>"`), fold
  duplicates (`close --duplicate-of <id>`); close only what the user said
  is done (`close --user-said`). CLAUDE.md rule 8.
- Add deadline estimates where possible
- Re-prioritize based on this week's findings
- Archive completed items older than 30 days

### 6.4. Project hygiene (the PARA pass)

Every project in the store has a `horizon`: `project` (an outcome and an
end), `area` (a standing responsibility) or `goal` (a long-term
direction). Areas and goals never go dormant and keep their chat room
as standing context. A `project` that has gone quiet is proposed for
dormancy, never demoted silently. The briefing JSON carries the list as
`project_hygiene.candidates` (computed in code, 30-day rule); to recheck:

```bash
python3 /home/ea-bot/ea-system/preprocessor/project_store.py dormancy-scan --days 30
```

Post the candidates under a `:file_cabinet: *Project hygiene*` line, one
per row: id, name, last activity date. Say that a reply of
`dormant <id>` (or `dormant all`) applies it and `keep <id>` leaves it
active; a dormant project's `#ea-` room is archived by the bot within
the hour and comes back when the project is revived
(`update-project --status active`). Do not apply dormancy yourself in
this run. If a candidate is plainly a standing responsibility, say so
and suggest `update-project --id <id> --horizon area` instead.

### 6.5. Closure scrubber health (the "user-stated closure is a bug" loop)

Run:
```bash
python3 /home/ea-bot/ea-system/preprocessor/closure_metrics.py report --days 7 --json
```

Report the numbers in the `:gear:` block of the chat template (below):
hit rate (autonomous / total closures), provenance split, scrubber
signal distribution, user-stated closures logged, and any suspected
false positives. If last week's report is visible in thread history,
note the hit-rate trend.

**Rule-proposal protocol.** If the report shows suspected false
positives OR >=2 user-stated closures sharing a `suggested_signal`,
propose a scrubber rule change — but ONLY with backtest numbers.
Run the relevant backtest first, e.g.:
```bash
python3 /home/ea-bot/ea-system/preprocessor/closure_metrics.py backtest \
  --days 90 --require-nongeneric-overlap
python3 /home/ea-bot/ea-system/preprocessor/closure_metrics.py backtest \
  --days 90 --add-confirmation-sender openreview.net
```
Then post:
```
:wrench: SCRUBBER RULE PROPOSAL #<YYYYMMDD>-<n>
Change: <one line, e.g. "add openreview.net to confirmation senders">
Evidence: <the log entries / cluster that motivate it>
Backtest (last 90d): would catch K missed closures, would suppress J
past auto-closes: <list the affected entry ids>
To adopt: reply "approve proposal <n>" — applied in an owner-side
Claude session (you cannot edit source; do not try).
```
Proposals WITHOUT real backtest output are forbidden. No false
positives and no repeated suggestions => write "no rule changes
proposed" and move on.

### 7. Gmail deep scan
Use Gmail MCP to search for:
- Receipts and travel confirmations (for reimbursement)
- Any threads related to open obligations
- Scheduling requests that may only exist in Gmail

### 7.5. If a PRE-SEND AUDIT RESULT block is in your prompt

Your previous report was checked in code against the mailbox and the
ledger, and was not posted. The rows the block lists as CLOSED are
closed: take them out of every open list and count them under closure
automation with the evidence in a few words. The rows it lists as
ambiguous stay exactly as they were, with nothing added to their line.
Change nothing else, and call no tool to re-check.

### 8. Post weekly report to chat
Write the report as your text output (the orchestrator posts it to the
EA chat channel automatically — do NOT call any chat send tool).

Before emitting each section, apply this pre-emission check to every bullet:
- **Critical / Important sections**: every item MUST correspond to a ledger
  entry with `status in (open, snoozed)` OR be derived directly from the
  current week's briefing. If the source entry is `closed`/`completed`,
  drop the bullet.
- **Completed this week**: every item MUST have `closed_at` within the last
  7 days. Older closures are not "this week."
- **Reimbursement status**: list active items only. An item with `status in
  (closed, completed)` is NOT active, no matter what its description says.
  If there are no active reimbursement items, write a single line:
  "No open reimbursement items." Do not fabricate activity.

Template:

```
📊 Weekly Audit – Week of [date]

🔴 Critical (action this week):
• [item with deadline or consequence]  ← open/snoozed only
• ...

🟡 Important (action soon):
• [item]  ← open/snoozed only
• ...

🟢 Completed this week:
• [item]  ← closed_at within last 7 days only
• ...

💰 Reimbursement status:
• [trip]: [status, what's needed]  ← open/snoozed reimbursement items only
  (OR: "No open reimbursement items.")

📈 Patterns noticed:
• [observation]

:gear: Closure automation (last 7d):
• Hit rate: [X]% ([N] autonomous / [M] manual) [trend vs last week]
• Provenance: scrubber [n], briefing [n], inbox-archive [n], manual [n]
• User-stated closures logged: [n]  ← each one is a bug; see proposals
• Suspected false positives: [list or "none"]
• [Rule proposal block(s) from step 6.5, or "no rule changes proposed"]

📋 Obligations: [open_count] open, [snoozed_count] snoozed,
   [closed_this_week_count] closed this week
```

### 9. Update state
- Save updated obligations ledger (propose stale items that look done, update notes)
- The weekly preprocessor resets delta tracking, so Monday morning will
  see ALL dropped threads as "new." The ledger's `notes` and `last_action`
  fields carry context between runs.

## Token budget
The weekly audit is the most expensive run (~30-50K tokens). It uses Opus
for deeper reasoning. To stay within budget:
- Fetch full message bodies for at most 10 messages (the most critical ones)
- Rely on body_preview + obligations ledger context for the rest
- The obligations ledger should already contain rich analysis from daily runs
