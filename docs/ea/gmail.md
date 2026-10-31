### Gmail drafts and sending

For emails sent from the user's Gmail (an address in `gmail_accounts` of
`user-profile.json`), create the
draft with the CLI:

```bash
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py create-draft \
  --email <gmail address> --to <addr> --subject "<subj>" \
  --body-file /home/ea-bot/.ea/state/reply-body-<ts>.txt
```

The Gmail MCP `create_draft` and `update_draft` are denied. The connector
rewrote every link in a body as a `google.com/url?q=` redirect, which the
recipient saw in place of the address and which stops working after a
day. The CLI writes plain text, so a link stays the address you wrote.
The CLI records the draft in the draft-events journal, and the bot posts
the `:lock:` Send/Cancel block from that record. You do not need to quote
the draft id in your reply. Add `--ledger-id <row id>` (on `create-draft`
or `update-draft`) when the draft delivers a ledger row, as on Outlook:
its Send closes the rows it names and the thread's reply row.

The Send card warns when a draft still has a redirect link (a
`google.com/url?q=` or SafeLinks address). Such a link usually comes from
text copied out of a calendar invite or a received email. Put the real
address in its place, which is the part after `q=` or `url=`, up to the
first `&`.

**Workflow:**
1. Create the draft (`gmail_client.py create-draft`)
2. The bot detects the draft and posts the `:lock:` block in the user's
   DM, with a pointer in the thread
3. User clicks Send -> bot calls `send-gmail-draft` behind the token gate
4. Do NOT call any Gmail send tool directly; `gmail_client.py send-draft`
   is refused for you

**When to use Gmail vs Outlook:**
- Outlook: work email (the institution's domain) -- default for all EA operations
- Gmail: personal email (gmail.com) -- only when the user explicitly
  asks to send from Gmail or the context requires it (personal contacts,
  non-work correspondence)

**Replies MUST name the message they answer.** A "Re:" draft created
without it opens a new conversation in Gmail (the recipient sees a
fresh thread, and the sender's own reply lands outside the exchange).
With the CLI pass the Gmail message id of the message you are answering
(from `search`, `get-message` or `get-thread`); `--to` and `--subject`
may be omitted, they default to the original sender and "Re: <subject>":

```bash
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py create-draft \
  --email <gmail address> --reply-to-message-id <gmail message id> \
  --body-file /home/ea-bot/.ea/state/reply-body-<ts>.txt
```

`--thread-id <gmail thread id>` answers a thread's newest message when
that is what you hold. The client sets threadId, In-Reply-To and
References; nothing else threads. It also quotes the message you answer
below your text ("On <date> <sender> wrote:", then its lines with `>`),
as Gmail does. Write only your new text in the body file. `--no-quote`
leaves the quote out.

To revise a draft use `update-draft --draft-id <id> --body-file <file>`,
with only the new text in the file. It keeps the draft's thread, its
quoted history and its attachments. `--reply-to-message-id` on it moves
a draft that was created as a new conversation into the right thread and
quotes that message. `--no-quote` drops the quote.

**Attaching a file to a Gmail draft:** the Gmail API has no attachment
endpoint, so use the CLI (SAFE, mediated). It rebuilds the draft in
place: same draft id, same thread, headers kept, existing attachments
kept:

```bash
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py attach-file \
  --email <gmail address> --draft-id <draft id> \
  --file /home/ea-bot/.ea/uploads/<file> [--file <another>] [--name <shown name>]
```

`--file` must sit under `~/.ea/uploads`, `receipts`, `docs`, `state` or
`/tmp` (the mediator refuses other paths; copy a letter into `~/.ea/docs/`
first). Files the user drops in chat are already under `~/.ea/uploads/`.
Never use `graph_actions.py attach-file` for a Gmail draft: it takes an
Outlook message id and cannot reach Gmail.

To take a file off a draft (to replace it, remove the old one and attach
the new one; never ask the user to do it by hand):

```bash
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py remove-attachment \
  --email <gmail address> --draft-id <draft id> --name <file name>
```

**Read flag:** `gmail_client.py mark-read --email <acct> --message-id <id>`
marks a message read in place (it removes the UNREAD label only);
`mark-unread` adds the label back.

**Reading Gmail attachments:** the Gmail MCP cannot fetch attachments. Use the
CLI (SAFE, mediated, works in every lane):
```bash
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py \
  list-attachments --email <acct> --message-id <id>
python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py \
  download-attachment --email <acct> --message-id <id> \
  --name-pattern "<filename substring>" --output ~/.ea/receipts/<trip>/<name>.pdf
```
`get-message` also returns an `attachments` list, so a message you have
already read tells you what is downloadable. Select with `--attachment-id`
(exact) or `--name-pattern` (case-insensitive substring); zero or several
matches is an error, not a guess. Under mail mediation `--output` must sit
inside `~/.ea/uploads`, `receipts`, `docs`, `state`, or `/tmp`.

**A Gmail message as a PDF** (a receipt with no attached PDF):
```bash
python3 /home/ea-bot/ea-system/preprocessor/email_to_pdf.py \
  --gmail <acct> --message-id <id> --output ~/.ea/receipts/<trip>/<name>.pdf
```
