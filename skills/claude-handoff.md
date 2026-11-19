# Skill: Claude handoff

The user takes a topic to the Claude app on their phone and talks it
through there, often by voice. You make the one file that Claude chat
starts from. Later the user pastes back a return block, and you carry it
out.

## When

- "Make a Claude handoff file for this conversation" (the web chat's
  Claude button sends these words), or any ask for a handoff doc, a file
  to talk over in Claude, or a package for a Claude chat or project.
- A message that starts with `TACITUS RETURN`: the return block from that
  chat (see "Carrying out a return block" below).

## The file

ONE Markdown file. Never a tarball, a zip or several files: the Claude
app on a phone takes neither archives nor a folder, and one file is one
tap to share. Everything the chat needs goes inside it as text.

- Path: `~/.ea/docs/<project>/<slug>-<YYYY-MM-DD>.claude.md`, with the
  project from the PROJECT INDEX block or `project_store.py match`;
  `~/.ea/docs/handoff/` when there is none. The name MUST end in
  `.claude.md`: that ending makes the bot attach it to your reply and
  gives the user the "Send to Claude" button.
- Size: under 300 KB. The documents under discussion go in full; when
  the material is larger, cut background first (summarize old mail, drop
  superseded drafts), never the documents themselves.

Gather before you write:
- The conversation so far: what the user asked, what you found, what was
  decided.
- The documents under discussion, in full and current. A document under
  review: `doc_review.py fetch --pr <n> --out <file>` (the pull request
  holds the current text). A file the user sent in this conversation, a
  file in `~/.ea/docs/<project>/`, a CV or a PDF:
  `python3 /home/ea-bot/ea-system/preprocessor/doc_text.py <path>` (it
  keeps a Word file's comments at their anchors; `--tracked` also marks
  tracked insertions and deletions).
- The project: `project_store.py pack --project <id>` (people, decisions,
  journal). Open rows: `ledger.py list --status open`, those on this topic.
- Mail on the topic: the facts and the asks, from the ledger rows' thread
  briefs and the records you already read. With sealed mail, use the
  records; never ask for raw text. Mail kept from hosted models stays out.
- Confidential review material (a manuscript or proposal under review,
  panel material, a recommendation or personnel letter, student records):
  ask the user before you put it in the file.

Write it in this order, in plain Markdown:

```
# <Topic>: talk-through
Prepared by Tacitus for <user's name>, <date>. From the conversation "<its title>".

## For Claude: how to run this conversation
- <Name> (<role>) wants to talk this through, often by voice on a phone.
  Keep each answer short enough to say aloud: two to four sentences, no
  tables, no long lists unless asked.
- Work through the open questions in order unless <Name> picks another.
  One question at a time. Propose an answer when you have one.
- When <Name> decides something, say it back in one sentence.
- When a change to a document is agreed, say the exact new wording.
- When <Name> says "wrap up", write the return block (format at the end
  of this file) inside ONE code block, with nothing before or after it.

## Where things stand
<three to eight sentences: the goal, the status, the deadline>

## Open questions
1. <question, with the options and your recommendation>
2. ...

## People and dates
- <name>: <role in this, what they want or owe>
- <date>: <what is due>

## Documents
### <document name> (<where it lives>, as of <date>)
<full text>

## Background
<earlier decisions with dates, mail gists, links>

## Return block format
TACITUS RETURN
handoff: <this file's name>
DECISIONS
- <one line each>
EDITS
- <document> | <section, or the first words of the passage> | <the change; the exact new wording in quotes when it was agreed>
TO-DOS
- <task> | <who> | <due date, or none>
EMAILS
- <to whom> | <what it must say>
OPEN
- <questions still open>
```

Your reply: the file's full path and one line on what is in it, for
example "`~/.ea/docs/bs-ai/bs-ai-proposal-2026-11-17.claude.md` has the
proposal, the 25 comments and the four open questions." Do not paste the
file into the chat. The user taps "Send to Claude" on the attached file
and picks Claude in the share sheet. When the topic has a ledger row,
record the file on it (`ledger.py update --id <id> --last-action`).

## Carrying out a return block

The user pastes the block from the Claude chat. Read every section, then
act; one reply reports it all.

- DECISIONS: `project_store.py journal --project <id> --kind decision
  --text "<decision>"`, one per line.
- EDITS: a document under review: `doc_review.py fetch`, apply every edit
  to that one copy, then one `doc_review.py update --message "Changes
  from the Claude talk-through"`. A file in `~/.ea/docs/`: edit it. A
  document only the user holds (a Word file they sent): a numbered punch
  list in your reply, in the order the edits appear in the document,
  with the exact new wording. An edit whose passage you cannot find or
  whose change is unclear: do not guess; list it under "Not applied"
  with the reason.
- TO-DOS: `ledger.py add --type task`, one row per task, with the due
  date when it has one. First check the open rows: a task that repeats
  one gets a `ledger.py update` on that row instead.
- EMAILS: draft each one by the normal drafting rules (the writer, the
  style profile, the Send button). Never send.
- OPEN: list them in your reply.

Reply with: what changed and where (the pull request link), the to-dos
added, the drafts made, and anything not applied.
