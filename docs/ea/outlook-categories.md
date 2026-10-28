# Outlook categories: the EA's labelling scheme

The EA labels mail with Outlook categories so a search or a filter in
Outlook groups a thread with the project it belongs to and says what,
if anything, the user still owes on it. Categories are set with
`graph_actions.py set-categories --message-id <id> --categories a,b,c`
(merges with what is on the message; `--replace` overwrites). The verb
is a SAFE mediated verb: a label leaves nothing the mailbox.

Every message the EA labels carries at most three or four categories:

1. `auto-tagged`, always. It marks the labels as the EA's, so a
   category the user set by hand is never mistaken for one of ours and
   a bulk re-label can target only our rows.
2. One project or domain tag. A project tag is the project store id,
   verbatim (`csci-4900`, `nsf-field-robotics`, `ms-curriculum`,
   `regional-consortium`), so the label joins with
   `project_store.py pack --project <id>` and nothing else maps names.
   When no project matches, the store's domain is the tag: `research`,
   `service`, `teaching`, `professional`, `personal`.
3. One state tag, which says what the thread wants from the user:
   - `needs-reply`: someone is waiting on an email from the user.
   - `todo`: an action other than a reply (a form, a registration, a
     review, a letter).
   - `decide`: a yes/no the user has to make before anything moves.
   - `scheduling`: a time has to be picked or confirmed.
   - `awaiting-reply`: the user wrote last; the thread is theirs now.
   - `fyi`: nothing owed. The usual companion of an archive.
   - `deadline`: a fixed date is in the thread. Combines with another
     state tag.
4. Optionally one topic tag for a recurring workflow that cuts across
   projects: `lor`, `recruiting`, `hiring`, `purchasing`, `funding`,
   `manuscripts`.

Colours. The Graph token holds `MailboxSettings.Read`, not
`ReadWrite`, so the EA cannot add names to the mailbox's master
category list. A new category shows uncoloured until the user gives it
a colour once in Outlook (Settings, Categories), after which every
message carrying it picks the colour up. Re-consenting the app with
`MailboxSettings.ReadWrite` would let the EA set colours itself.

State tags are the EA's to keep current: when the user's reply goes
out, `needs-reply` comes off and `awaiting-reply` goes on; when the
ledger row closes, the state tag becomes `fyi`.

## The tagger

`preprocessor/mail_tagger.py tick` runs from the owner's crontab every
two minutes (`mail_tagger.enabled` in ea-config.json; `model` names the
CLI model, `haiku` by default). A tick lists the inbox since the last
tick, sends the rows without `auto-tagged` to the model in one
tool-less call, validates every answer against the vocabulary above and
PATCHes the categories; a message with no usable verdict is retried on
later ticks and after three misses gets `auto-tagged` alone. The same
tick lists Sent Items since the last tick and, for every conversation
the user wrote in, replaces `needs-reply` and `scheduling` on the
thread's inbox messages with `awaiting-reply`. A tick with nothing new
is two Graph GETs and no model call. `backfill --days N` labels older
inbox mail; `status` shows the state file
(`~/.ea-private/mail-tagger.json`, log beside it as `.jsonl`).

The project tag comes from the project store. The prompt lists each
active project with its parent, aliases, the addresses of its people and
the first sentence of its summary, so the model can tell a child project
from its parent and see who works on what. It names a project only when
the message is about that project's work, and a domain word otherwise.
A sender's earlier labels carry only the topic tag forward: the project
call is made fresh for every message, so one wrong call does not follow
a sender. Better project summaries and people lists in the store make
better tags.
