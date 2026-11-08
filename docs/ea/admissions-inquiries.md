### Prospective-student inquiries (the admissions-inquiries folder)

This feature is on only where `~/.ea/config/ea-config.json` sets
`"admissions_filter": {"enabled": true}`; elsewhere none of this runs.

Cold email from prospective PhD and MS students is not an obligation and
is not triaged in a briefing, a dispatch or the ledger.
`preprocessor/admissions_filter.py` moves each one out of the inbox into
the Outlook folder `admissions-inquiries` within five minutes of arrival
(a cron, around the clock), and again at the start of every preprocess
mode, so no detector turns one into a needs-response row.

What it moves: an outside sender whose subject or new text shows
applicant intent ("Prospective PhD Student, Fall 2027", "seeking PhD
positions", "applying to MS programs", "join your lab", CV attached).
What it never moves, whatever the text says: an internal sender (the
institution's domains and related campuses); anyone the
user has written to (the style profile store and the last 30 days of
sent mail, by address or by name); a conversation the user sent in; a
message with an internal colleague on it; a reply to something the user wrote;
a third party introducing a student; a configured mailing list; a VIP.

When the user answers one of these students (the Send button, "Sent
myself", or a send straight from Outlook that the bot notices), the
conversation's inbound mail, inbox and archive alike, moves to the folder
too (`admissions_filter.py file-conversation`); the user's sent copies
stay in Sent Items. `file-senders --sender <addr>` files everything from
named people at once.

A move also closes the detector-made ledger rows (`response-`, `thread-`)
of that conversation, in case an earlier pass saw the mail first.

When the user drags a message back to the inbox, the next sweep sees
it and never moves that sender or conversation again. Every move is a
line in the owner-private log `admissions-filter.jsonl`.

Once a week (Monday 08:00) the `admissions-digest` agent reads the
folder's last seven days and posts one ranked summary: a short "worth a
look" list and the rest by name. It drafts, answers and moves nothing.
If the user says a judgment was wrong ("that one was worth a look",
"never shortlist X"), record it with `preferences.py add --scope
admissions ...` or `preferences.py calibrate --scope admissions`; never
edit the skill. If the user asks to answer one of these students, treat
it as an ordinary drafting request on that message.

#### Keep levels: what you may see of applicant mail

Applicant mail stays on the user's machine. No hosted model reads it:
you, Claude or Codex alike. Its files are never read. After the user
writes to an applicant, that person's mail is kept at `summary`. You get
the local model's record (summary, ask, deadline, offered times), never
the text. An item, a read or a thread brief marked `kept_level` names the
level: "Kept on your machine", "Facts only" or "Summary". Track the
thread, draft through the writer and set up the meeting as usual. Do not
ask for the text or the files. The user reads them in Outlook.

When the user says how to treat a person or a domain, record a privacy
rule in the same turn. Write the text as a plain sentence:

```
python3 /home/ea-bot/ea-system/preprocessor/preferences.py add --kind instruction --scope privacy --text "<plain sentence>" --quote "<their words>" --match person:<address> --level summary
```

`--level` is `local`, `facts`, `summary` or `full`. `--match` is
`person:<address>`, `name:<first and last name>` or `domain:<domain>`. A
domain rule only keeps mail closer (`local` or `facts`). `preferences.py
list --scope privacy` shows the rules in force.

To find a student's mail in a conversation, read the folder by its name:
`graph_actions.py list-mail --folder admissions-inquiries` (newest
first) or `mail_actions.py search --provider outlook --folder
admissions-inquiries --query <name>`.
