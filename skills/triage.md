# Skill: Triage Inbox Items
<!-- scopes: mail-triage -->

Interactive skill for batch-processing inbox items. The user gives
instructions for multiple items at once (archive, reply, schedule, etc.)
and this skill executes them sequentially with brief confirmations.

## Usage

User says something like:
- "Archive the journal review emails, they're handled. Archive the dean eval too."
- "Triage: archive Sage, reply to Sol saying good progress, skip the ACM review"
- "Clear out the stale stuff — anything over 60 days with no follow-up, archive it"

## Steps

### 1. Parse the batch
Break the user's message into individual action items. For each, identify:
- **Action**: archive, reply, skip, calendar event, mark done
- **Target**: which email/obligation (by sender name, subject keywords, or ledger ID)
- **Details**: any specifics (reply content, calendar time, etc.)

List them back to the user for confirmation:
```
I'll do these 4 actions:
1. Archive journal review reminders (2 emails)
2. Archive dean evaluation survey
3. Reply to Sol — "good progress, keep going"
4. Skip ACM IoT review

Proceed? Or edit the list.
```

### 2. Find each email
For each action that targets an email:
```bash
python3 ~/ea-system/preprocessor/mail_actions.py search --provider outlook --query "<keywords>"
```

Also check the obligations ledger for message IDs already tracked.

### 3. Execute actions

**Archive:**
```bash
python3 ~/ea-system/preprocessor/graph_actions.py archive-message --message-id <id>
```

**Reply:** Follow the reply.md skill flow (search → read → draft → preview → send).
For batch replies where the user gave the content, draft and preview all
at once rather than one at a time.

**Calendar event:**
```bash
python3 ~/ea-system/preprocessor/graph_actions.py create-event \
  --subject "<title>" --start "<iso>" --end "<iso>" --attendees "<emails>"
```

**Mark done:** the user asked for it, so close the row with their words:
`ledger.py close --id <id> --note "<reason>" --user-said "<their words>"`.

### 4. Report results
After executing, give a concise summary:
```
Done:
- ✓ Archived journal review reminders (2 emails)
- ✓ Archived dean evaluation
- ✓ Draft reply to Sol created — check Outlook Drafts
- ✓ Skipped ACM IoT (removed from ledger)
```

### 5. Update state
- Update obligations ledger for all affected items
