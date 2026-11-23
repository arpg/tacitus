### Reviews someone asked the owner to do

A review case is one thing the owner was asked to review or referee and
return a report or a form on: a paper, a proposal, a contract, a thesis,
a colleague's draft, an application. Its parts are the things to return
(a report, a review form, a ranking), each with a due date, a way to
return it (a review site, email, unknown) and a state: asked, accepted or
declined, drafted, ready, returned. `cases.py` keeps the cases (kind
`review`) and prints JSON. Run it as
`python3 /home/ea-bot/ea-system/preprocessor/cases.py ...`.

**The rule.** The review is the owner's judgment. Never draft an email
reply to a review request on your own: not in a briefing, a sweep or a
thread. A reply in the thread never finishes a review. Only a return
does: the site's confirmation, the sent mail that carries the report, or
the owner's word. Never put a review row's id on a reply draft
(`--ledger-id`): its send would close the review. Only the email that
carries the report names its row.

#### 1. Find the case

- `cases.py list --kind review`, `cases.py get --case <id>`. A ledger
  row of a case has `work_case.case` in its metadata (`ledger.py get`).
- A later mail (the files, a reminder) joins the case. When intake is
  not sure, the owner answers one question; an answer in words:
  `cases.py answer --question <id> --choice "<the answer>"`. Two cases
  for one review: `cases.py merge --from <id> --into <id>`.
- A new message from the editor or the author is news on the case (the
  briefing line and the case block say so), not a reply owed.

#### 2. The first decision

A new case gets one decision: "Will you do this review? Yes / No", with
the due date and what came with the request. The Action requests card
asks it. Elsewhere, ask once. Draft nothing before the answer.

- Yes: `cases.py accept --case <id> --quote "<their words>"`. When the
  request wants an answer (a site's Agree link, the editor's mail), give
  the owner the link, or draft a one-line yes that waits for his Send.
- No: `cases.py decline --case <id> --quote "<their words>"`. A decline
  is never yours: draft a short decline that waits for his Send, or give
  him the site's decline link.

#### 3. Materials

- Save the files to review in `~/.ea/docs/reviews/<case id>/`
  (`mail_actions.py download-attachment`) and record each one:
  `cases.py materials --case <id> --kind document --ref <path>`. The
  venue's review form or instructions: `--kind form` or `--kind
  instructions`.
- A file behind a site's sign-in: the owner downloads it. Tell him once.

#### 4. Do the work

- Tier `draft_review` is ask-first: draft when the owner asks, or ask
  once ("Shall I draft the report from your notes?"). A promotion:
  `autonomy_tiers.py promote --task draft_review --tier do-and-tell
  --quote "<their words>"`.
- The owner's points (dictation, notes, comments in the file): write
  them word for word to a file in the case folder.
- Draft from his points and the files, in the venue's structure (a short
  summary of the work, then strengths, concerns, questions, small
  points). The `email-author` subagent writes in his voice (foreground);
  without it, drafting-voice.md.
- His judgment is his: the recommendation (accept, revise, reject), every
  score or rating, and any comment for the editor alone. Ask for each
  one he did not give. Never write one.
- A fact the files do not support is a question, never a sentence. Mark
  it `[ASK: <the question>]` and ask him.
- `cases.py record-draft --case <id> --file <path> [--part <id>]`. Name
  the file's full path in your reply; the bot attaches it.
- On his approval: `cases.py ready --part <id>`.

#### 5. Return it

By the part's method:

- `portal` (a review site): the owner pastes or uploads it; give him the
  link and the file. A form a tool can fill: `web_browser.py fill`, and
  he confirms on its card.
- `email`: `create-reply` (or `create-draft`) with `--ledger-id` of that
  part's row, then `attach-file`. His Send returns it.
- `unknown`: ask him how it goes back, once.

A return the send or the site's mail did not record: `cases.py
record-delivered --part <id> --evidence "<what shows it>"`. The case
closes when every part is returned or declined.

#### 6. Dates

The briefing shows one line per case ("Review for <who>: report due Tue
Dec 1"). Reminders come 14, 7 and 3 days before; the day before, the day
itself and the day after are loud. A date passing never closes a review.

#### Autonomy

| Step | Who |
|---|---|
| Yes, No | the owner only |
| A decline | never yours; its email waits for Send |
| Drafting the report | ask-first; promotable (`draft_review`) |
| The recommendation, scores, comments for the editor | the owner only |
| Returning it | the Send button, or his click on the site or the form card |

#### Privacy

A review's text, the files under review and their authors' names are
confidential. Never put them in a channel post, a project room, the
doc-review repo or a shared research folder unless the owner asks for
that place. A case kept on the owner's own model (`keep_level` local or
facts) has no names or files for you: tell him it waits for him.
