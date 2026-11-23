### Letters someone asked the owner to write

A letter case is one person the owner was asked to write for: a
recommendation, a reference, a nomination, a support or a character
letter, for a student, an employee, a colleague, a tenant or a client.
A case has destinations (a program, an employer, an award, a portal),
each with a due date, a submission method and a state: asked, accepted
or declined, drafted, ready, submitted. `letters.py` keeps the cases;
`letters_work.py` does the work. Both print JSON. Run each as
`python3 /home/ea-bot/ea-system/preprocessor/<script>.py ...`.

**The rule.** Never draft an email reply to a letter request on your
own: not in a briefing, a sweep or a thread. The work is the letter, by
its date. A reply in the thread never finishes a letter. Only a
submission does: the portal's confirmation, the sent mail that carries
the letter, the upload tool's success, or the owner's word. Never put a
letter row's id on a reply draft (`--ledger-id`): its send would close
the letter. Only the email that carries the letter names its row.

#### 1. Find the case

- `letters.py list`, `letters.py get --case <id>`. A ledger row of a
  case has `letter.case` in its metadata (`ledger.py get --id <id>`).
- `letters_work.py status --case <id>`: each destination's state and
  next step, what is missing (materials, dates, methods), open form
  questions, the tiers, and `decision` while nobody has answered.
- Intake joins a later mail to the case of the same person. When it is
  not sure, the owner answers one join question; an answer in words:
  `letters.py answer --question <id> --choice "Same person"` (or
  "Someone else"). Two cases for one person: `letters.py merge --from
  <id> --into <id>`.

#### 2. The first decision

A new case gets one decision: "Will you write it? Yes / Yes, ask for
materials / No", with the dates and what the request already included.
Its Yes carries the case's one date ("Yes, by Tue Dec 1": the earliest
date a place states, or "Yes, by another date"). The web app's Action
requests card asks it. Elsewhere, post the `decision` line from `status`
once. Draft nothing before the answer.

On the answer (a button or the owner's words), run
`letters_work.py decide --case <id> --answer yes|materials|no --owner-said "<their words>"`:

- `yes`: the case is accepted. No email. Add `--reply` only when the
  owner's words or a standing rule ask for a yes email.
- `materials`: accepted, and a reply brief that asks only for what the
  case does not hold. `--want "cv, statement"` when the owner named
  items. With nothing recorded, it reads the request's attachment list
  first. Always tell the owner `already_received`. Nothing missing: no
  email.
- `no`: every open destination is declined and its row closes. It writes
  a short decline brief. `--no-reply` when the owner says not to answer;
  `--after-yes` when they had said yes; `--dest <id>` for one place.

Then run its `next` steps: the writer, then `create-reply` with no
`--ledger-id`. The draft waits for the owner's Send. `decide` refuses
without the owner's words: you never decide a case, and a decline is
never yours.

#### 3. Materials, places and dates

- `letters_work.py collect --case <id>` saves the request's files in
  `~/.ea/letters/<case>/materials/` and records them. Gmail: add
  `--provider gmail --account <address>`.
- A place that arrives after the yes (a portal's mail) joins as
  accepted. `letters_work.py extend --case <id> --dest <id>` makes its
  copy of the letter. Tell the owner in one line. It needs no new
  decision.
- The case has one date (section 7). Never ask the owner for a date per
  place.
- What a place asks for: `letters_work.py require --case <id> --dest
  <id>` with `--length`, `--format`, `--addressee`, `--program`,
  `--organization`, or a form question: `--question "<text>" --options
  "a|b|c"` (add `--rating` for a rank or a percentile). Ratings and form
  answers are the owner's. Ask, then record with `letters_work.py
  answered --case <id> --dest <id> --question q1 --value "<answer>"
  --owner-said "<their words>"`. Never guess one.

#### 4. Draft the letter

- Tier `draft_letter` is ask-first. Without the owner's ask, `brief`
  returns `ask`: post it and wait. A promotion: `autonomy_tiers.py
  promote --task draft_letter --tier do-and-tell --quote "<their words>"`.
- The owner's dictation or notes: write them word for word to a file in
  `~/.ea/state/` and pass `--notes-file`.
- `letters_work.py brief --case <id> --owner-said "<their words>"
  [--notes-file <file>] [--dest <id>]` writes `brief.txt` in the case
  folder: the owner's points, the materials with excerpts, earlier
  letters for this person and of this kind, each place's needs, and the
  rules.
- Call the `email-author` subagent in the foreground: "Write the letter
  in <brief path>. Read every file it names. Return the letter body
  only." Write its text to the brief's `out` file. Do not add claims.
- `letters_work.py check --case <id> --file <out> --record` records the
  draft and lists `questions` (its `[ASK: ...]` marks). A letter is the
  owner's evaluation: a fact the materials do not support is a question
  for the owner, never a sentence. Ask them, put the answers in the
  letter (Edit), and check again.
- One letter for several places uses `{{program}}` and `{{addressee}}`.
  `letters_work.py variants --case <id> --base <letter.md>` makes one
  copy per place and refuses a copy that still names another place. A
  letter written for one place: add `--base-dest <id>` to swap its
  names. A letter that names no place serves them all.

#### 5. Review and approval

- `letters_work.py render --case <id> --file <copy> [--dest <id>]`: a PDF
  on the owner's letterhead when the profile's `institution.letterhead`
  has a `template.tex`, a plain PDF otherwise. `--docx [--template
  <file>.docx]` gives a Word file.
- The owner reads the letter as a file: name the PDF's full path in your
  reply, and the bot attaches it. Never paste the letter text into chat,
  a project room, a doc-review pull request or a shared research folder
  unless the owner asks for that place.
- On the owner's approval: `letters_work.py approve --case <id> [--dest
  <id>] --owner-said "<their words>"`. It refuses a letter with questions
  left, placeholders, another place's name, no PDF, or form questions
  not answered. A change after approval: edit, render, approve again.

#### 6. Submission

`letters_work.py submit --case <id> --dest <id>` gives the steps:

- `email`: the writer's short cover note, `create-draft` to the address
  with `--ledger-id` of that place's row, then `attach-file` with the
  PDF (`gmail_client.py attach-file` for Gmail). The owner's Send closes
  the place.
- `portal`: tier `upload_letter_of_recommendation` (do-and-tell).
  `web_browser.py read`, then `fill` with the form answers and an upload
  action; the owner confirms on its card. A plugin's upload verb, when
  its skill names this portal. A sign-in page: the owner uploads it.
- `upload` or `unknown`: give the owner the link and the file path.

A submission the send or the portal's mail did not record:
`letters.py record-submitted --dest <id> --evidence "<what shows it>"`.
The case closes when every place is submitted or declined.

#### 7. One date, then quiet until it is time

The owner writes one letter by one date and uploads it to each place.
The case has that one date (`do_by`): every place goes under it, and a
place's own later date is kept but not shown until the letter is ready.
A place that comes with an earlier date moves the case's date; the owner
is told once. Once the letter is ready and the date has passed, a place
still open is late only against its own date.

- The owner gives the date with his yes. A yes with no date takes the
  earliest date a place states, and the bot tells him in one line. Only a
  case with places and no date anywhere gets one date card; a case with
  no place yet waits, silent, until a place or a date comes. In words:
  `letters.py set-date --case <id> --date YYYY-MM-DD`.
- Until 7 days before the date the case is quiet: no card, not on Home,
  not in the daily briefing (the weekly review lists it). "Start this
  one now": `letters.py start --case <id>`. "Remind me 3 days before":
  `letters.py lead --case <id> --days 3`. The owner's setting for every
  letter: `case_lead_days` in `ea-config.json`.
- At its time the case's block says "Time to write the letter". Near the
  date with no draft: ask to draft (ask-first), or draft and tell
  (do-and-tell). Once approved, the block is the delivery list.
- A place that asks after the letter is ready shows as one line. A date
  passing never closes a letter.

#### Autonomy

| Step | Who |
|---|---|
| Yes, ask for materials, No | the owner only (`decide` needs their words) |
| A decline | never yours; its reply waits for Send |
| Drafting the letter | ask-first; promotable (`draft_letter`) |
| Uploads | do-and-tell; a form's submit is the owner's click |
| Any email | the Send button |

#### Privacy

A case kept on the owner's own model (`keep_level` local or facts) has
no names, files or text for you: `letters_work.py` refuses the content
verbs. Tell the owner the letter waits for them. Earlier letters for
other people serve voice and length only, never facts.
