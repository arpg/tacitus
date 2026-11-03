# Skill: Reply to Email
<!-- scopes: drafting -->

Compose and create an Outlook reply draft in one pass. The bot's
`:lock:` Send/Cancel block handles user approval — this skill never
previews drafts in chat.

## Steps

### 1. Parse the request
Extract: recipient, thread keywords, intended content. If the user
provided rough notes, polish them. If they provided finished text,
format it cleanly but don't rewrite.

### 2. Find the email
```bash
python3 /home/ea-bot/ea-system/preprocessor/mail_actions.py search --provider outlook --query "<keywords>"
```
If one clear match, proceed. If multiple matches, pick the most recent
relevant one. Only ask the user if genuinely ambiguous (same sender,
multiple active threads on different topics).

### 3. Read the thread for context
```bash
python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py get-conversation \
  --conversation-id <conversation_id from the search row>
```
One call returns every message in the thread, oldest-first, with quoted
history stripped. Use `get-message --message-id <id>` only when you want
that ONE message and nothing else. Never run a get-message per search
hit: search matches loosely, so some hits are not in the thread.

### 4. Draft the body
Compose based on:
- The user's instructions (content, tone, key points)
- The original email context (who else is on the thread, what was asked)
- Writing style from the briefing's `writing_style.per_contact` (exact
  greeting, closing, length, formality). If no per-contact data, use the
  `default_*` values.

**Scheduling-aware replies**: For any time-proposing reply, use
`propose-meeting` (see `scheduling-rules.md`). Trust its output.

### 5. Create the draft
Write the body text to the per-invocation body file path shown in the
Tools section of the prompt, then call create-reply with --body-file:
```bash
# Step A: Write the final body text to the body file (shown in Tools block).
# Step B: Create the reply:
python3 /home/ea-bot/ea-system/preprocessor/mail_actions.py create-reply --provider outlook \
  --message-id <id> --conversation-id <conversation_id> --body-file <body-file-path> \
  --ledger-id <row id>
```
`--ledger-id` names the ledger row this reply delivers (repeat it for
more rows); its Send closes those rows and the thread's reply row,
and any other row on the thread goes on the confirm card. Leave it out only when
no row is tracked for this reply.
Use reply-all by default. Use `--sender-only` only for scheduling poll
responses (don't broadcast your availability) or when the user
explicitly asks.

ALWAYS pass `--conversation-id` (from the ledger / briefing item) when you
have it. A stored `message-id` can be stale: archiving or moving the source
email changes its Outlook id, so create-reply would 404. With the conversation
id, create-reply re-resolves the live message and keeps the quoted thread.
If a reply ever fails, NEVER fall back to a plain `create-draft`: a reply
without the quoted history is wrong. Re-resolve the id (search by
conversation/subject) and retry, or report the failure.

Save the returned draft_id.

### 6. Confirm briefly — do NOT preview
Post ONE short line to chat: `:email: Draft created to [recipient] re: [subject].`

The bot's `:lock:` Send/Cancel block auto-appears with the full draft
body for user review. That block IS the approval step. Do NOT:
- Post the draft body text to chat (it's already in the :lock: block)
- Ask "want to change anything?" or "should I create the draft?"
- Call send-draft (the bot handles sending after the user clicks Send)

### 7. If the user asks for changes after draft creation
Use update-draft with the SAME draft_id. NEVER create a second draft:
```bash
python3 /home/ea-bot/ea-system/preprocessor/mail_actions.py update-draft --provider outlook \
  --draft-id <draft-id> --body-file <body-file-path>
```
This updates the existing draft in-place.
