### Project store (context, not tasks)

`preprocessor/project_store.py` is the relational memory over the same
SQLite database: projects (research / teaching / service / professional /
personal, with parent hierarchy), people (joined to style profiles for
tone), ledger links, filed email gists, and an append-only journal.
The ledger answers "what do I owe"; the store answers "what is this
about and who is in it".

- `pack --project <id>` is the monolithic read: summary, people with
  roles and voice, open ledger items, journal, recent mail — sized for
  one prompt injection. Dispatches may already carry an auto-matched
  pack; do not re-fetch what is injected.
- Domain is which HAT (a course on AI is teaching even though it sounds
  like the user's AI program role); parent_id is which UMBRELLA
  (ms-curriculum under program-director). Do not conflate them.
- Horizon is the PARA distinction: `project` has an outcome and an end
  and can go dormant; `area` is a standing responsibility (an AE role,
  a lab's fleet, an advising relationship); `goal` is a long-term
  direction (a move into research leadership). Areas and goals keep their
  chat room and never go dormant on their own. When you create a
  project, set `--horizon`; when a "project" has no end, it is an area.
  `dormancy-scan` lists quiet projects; the weekly audit proposes them
  and the user decides. A dormant project's room is archived by the bot
  and comes back on `update-project --status active`.
- A project created during a chat conversation gets its `#ea-<id>`
  room right after the turn: the bot opens it, invites the user, posts
  the project's context there and the link in the thread. Creating the
  row is the whole request; do not tell the user to say `open a
  channel`. Areas and goals get rooms too; a project created with
  `--status` other than active does not. For a project that already
  exists, stage the room: write `~/.ea/state/pending-channel.json` with
  `{"project": "<id>", "thread_ts": "<thread>"}` and the bot opens it
  after the turn. Never announce a room you did not stage.
- A room request naming no known project (`open a channel for
  harbor-survey` when no such row exists) gets a picker, not a refusal:
  Open buttons for the closest projects (`project_store.py suggest
  --text ...` is the same ranking), a menu of every active project, and
  a Create button. Create dispatches one instruction into the thread:
  register the project from the conversation with `add-project`, link
  its ledger items, people and URLs, and say nothing about a room; the
  room opens by itself once the row exists. `open a channel for this
  project` reads the thread (its text, and the ledger ids the EA named
  in backticks) to find the project; one clear winner opens, anything
  else is the picker.
- WRITE AS YOU GO: `journal --project <id> --kind decision|milestone
  --text "..."` when something is decided or lands;
  `file-message --project <id> --conversation-id <cid> --subject ...
  --gist "..."` when a notable email belongs to a project;
  `link-person` / `add-person` when someone new appears. The store rots
  without this, and a rotted store is worse than none.
- `match --text "..."` finds the project for a message (aliases, names,
  members). Use it before filing rather than guessing ids.
- Aliases place mail in a project's room and link ledger rows to it.
  Write a name, an acronym or a phrase as it is written (`Northwind`,
  `ORION`, `foundation repair`). One lowercase word (`foundation`) is a
  common word: alone it places nothing, because unrelated mail carries
  it too.
- `index` prints every active project on one screen (id, name, parent,
  open items, next action). Dispatches that carry a project pack, come
  from a project or session room, or talk about filing get it appended
  as PROJECT INDEX. A room is scoped to one project; the work is not
  always. When something belongs to another project, file or journal it
  there and say so.

The obligations ledger is SQLite-backed. Use the CLI tool to manage it:
```
python3 /home/ea-bot/ea-system/preprocessor/ledger.py add --id <id> --type <type> --description "..." [--from-email <addr>] [--source-message-id <id>] [--conversation-id <id>] [--notes "..."]
# <type>: response_needed, follow_up, scheduling, task, deadline, waiting,
# reimbursement, dispute or draft_pending (`ledger.py types`)
python3 /home/ea-bot/ea-system/preprocessor/ledger.py update --id <id> [--status <s>] [--last-action "..."] [--notes "..."] [--conversation-id <id>] [--email-archived]
python3 /home/ea-bot/ea-system/preprocessor/ledger.py close --id <id> --note "reason" --user-said "<their words>" [--skip-archive]
python3 /home/ea-bot/ea-system/preprocessor/ledger.py propose --id <id> --why "<evidence it looks done>"
python3 /home/ea-bot/ea-system/preprocessor/ledger.py list --status open
python3 /home/ea-bot/ea-system/preprocessor/ledger.py get --id <id>
python3 /home/ea-bot/ea-system/preprocessor/ledger.py search --query "text"
python3 /home/ea-bot/ea-system/preprocessor/ledger.py stats
```
Do NOT read or edit the .db file directly. Each CLI call is atomic.
Entries are NEVER deleted — `close` sets status to completed.
`close` auto-archives the email from inbox (no separate archive call needed).
Use `--skip-archive` when the email is already archived (ledger_sync, user action).

If a Write or Edit fails with permission denied, it means you are
trying to modify a file you should not.

### Shared research folders

`~/.ea/research/<topic>/` is a folder the user and the EA both write
(group `ea`, setgid, default ACLs, so a file either side creates stays
editable by the other). The user's research tree keeps a symlink to each
folder they moved in, so their tools see nothing changed. The project record
names the folder in `instructions`; `pack` therefore carries the path.
When a message is about that project: read the folder's `CLAUDE.md`
first (the user's research repos each carry one), then the code, results and
notes it points at. The EA's own notes for the project stay in
`~/.ea/docs/<project>/`, linked into the folder as `ea-notes/`. Write
new files beside the user's (a note, a figure, a script) and journal what
you added; rewrite one of their files only when they ask. The folder is an
allowed file root for `attach-file` and `download-attachment`.
