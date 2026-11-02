# Skill: Context Export

Generate a self-contained context briefing from a collected export manifest.
The output should give someone in a separate Claude conversation full context
to continue working on this topic without access to the EA system.

## Trigger

User says "export this", "dump the email context", "gather context", etc.
Two modes:

**In-thread:** The bot runs the three-phase collect/summarize/package flow
automatically. You receive a manifest and write a briefing (see Steps below).

**Top-level (topic dump):** The user asks to dump context about a topic
(e.g., "dump the email context about the vendor pilot"). You must:
1. Search the obligations ledger: `python3 preprocessor/ledger.py search --query "<topic>"`
2. Search Outlook for relevant emails: `python3 preprocessor/mail_actions.py search --provider outlook --query "<topic>" --top 10`
3. Read the full bodies of the most relevant emails (use `get-message`)
4. Write the export to `~/.ea/exports/<topic>-<date>/context-export.md`
5. Post a confirmation with the file path so the user can retrieve it:
   ":package: Context export for [topic] saved to `<path>`. Download from the server or I can paste key sections here."

For top-level dumps, build the same self-contained markdown structure
described below, but populate it from your email/ledger searches rather
than from a manifest.

---

## In-thread mode: What happened before you were called

The bot already ran `context_export.py collect` which gathered:
- Thread messages from chat
- Full email bodies for any Outlook messages referenced in the thread
- Downloaded email attachments to disk
- Matched obligations ledger entries

All of this is in a manifest JSON file. Your job is to read it and write
a well-structured briefing markdown.

## Steps

### 1. Read the manifest

The manifest path is provided in the prompt. Read it. It contains:
- `thread_messages`: the chat thread conversation
- `emails`: list of email objects with subject, from, to, date, body_text, attachments
- `ledger_entries`: related obligation tracker entries with status, deadlines, notes
- `has_attachments`: whether attachment files were downloaded
- `exported_at`: timestamp

### 2. Write the briefing markdown

Create a markdown file that is **completely self-contained**. Someone reading
this in a separate Claude conversation with NO access to the EA system, email,
or the chat should understand the full context and be able to continue the work.

Write to the path specified in the prompt.

Structure:

```
# Context Export: [Topic — infer from thread + emails]
Exported: [date]
Source: EA chat thread

## Summary
[2-3 sentences: what this is about, who is involved, current status]

## Conversation Thread
[Cleaned thread messages. Remove:
 - Usage footer lines (session/weekly percentages)
 - "Working on it..." status messages
 - Bot metadata and formatting artifacts
 Keep the substantive conversation between user and EA.]

## Email Content
[For each email in the manifest:]

### [Subject] (from [Name], [Date])
To: [recipients]

[Email body text, cleaned of HTML artifacts and excessive whitespace]

---

[Repeat for each email]

## Tracked Items
[For each ledger entry:]
- **[description]** — Status: [status], Due: [due_by]
  [Include notes if they add context not already covered above]

## Attachments
[If has_attachments is true, list each attachment:]
- `filename.pdf` — [brief description based on name and context]

[If no attachments, omit this section]

## Key Decisions
[Bullet points of decisions made in the thread conversation]

## Open Items
[Anything still pending, awaiting response, or requiring follow-up]
```

### 3. Keep it concise

- Do not pad with boilerplate. Every line should carry information.
- Strip quoted reply text from emails (the "On [date] [person] wrote:" blocks)
  unless the quoted text contains information not in the direct email bodies.
- For ledger entries, only include what adds context beyond the emails.
- If the thread is short (< 5 messages) and covers one email, consolidate
  rather than repeating information across sections.

### 4. Report to chat

Post a short confirmation to chat: ":package: Context export ready. Uploading now."

Do NOT post the full briefing to chat. It will be uploaded as a file.
