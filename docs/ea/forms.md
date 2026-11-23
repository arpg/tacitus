### Forms someone asked the owner to fill in

A form case is paperwork the owner must fill in and return by a date: a
form, a survey with a deadline, a signature, a disclosure, a compliance
training, a registration. Its parts are the forms to submit, each with a
due date, a way to submit it (a web form, email, an e-signature service,
unknown) and a state: asked, accepted or declined, drafted (filled in,
not checked), ready, submitted. `cases.py` keeps the cases (kind `form`)
and prints JSON. Run it as
`python3 /home/ea-bot/ea-system/preprocessor/cases.py ...`.

**The rule.** A form is the owner's statement. Never submit one on your
own and never sign one. Never draft an email reply to a form request on
your own. A reply in the thread never finishes a form. Only a submission
does: the service's confirmation, the sent mail that carries it, or the
owner's word. Never put a form row's id on a reply draft.

#### 1. Find the case

- `cases.py list --kind form`, `cases.py get --case <id>`. A ledger row
  of a case has `work_case.case` in its metadata.
- A reminder from the same office or service joins the case. When intake
  is not sure, the owner answers one question (`cases.py answer
  --question <id> --choice "<the answer>"`).

#### 2. The first decision

"Will you fill it in? Yes / No", with the date. The Action requests card
asks it; elsewhere, ask once.

- Yes: `cases.py accept --case <id> --quote "<their words>"`.
- No: `cases.py decline --case <id> --quote "<their words>"`. Nothing is
  sent. A reply that he will not do it is his to ask for, and waits for
  his Send.

#### 3. Materials

- The form itself (an attachment, a link) and its instructions: save
  them in `~/.ea/docs/forms/<case id>/` and record each one: `cases.py
  materials --case <id> --kind form|instructions --ref <path>`.
- Earlier forms of the same kind that the owner approved are in the same
  place. They give his usual answers, never new facts.

#### 4. Fill it in

- Tier `fill_form` is ask-first: fill in when the owner asks, or ask
  once. Promotable: `autonomy_tiers.py promote --task fill_form --tier
  do-and-tell --quote "<their words>"`.
- Fill in only what the records show: his profile (name, title,
  department, work address), an earlier form he approved, the materials.
- Every other field is a question for him: a signature, a date he
  attests, a yes or no about himself (a conflict of interest, outside
  work, money, health, travel), any number. Never guess one.
- Write the answers to `answers.md` in the case folder, with the open
  questions listed first, and name its full path in your reply.
  `cases.py record-draft --case <id> --file <path>`; on his approval,
  `cases.py ready --part <id>`.

#### 5. Submit it

By the part's method:

- a web form: `web_browser.py fill` with the approved answers; he
  confirms on its card (a form write is button-required).
- an e-signature service or a training: give him the link. He signs or
  takes it himself.
- `email`: `create-draft` or `create-reply` with `--ledger-id` of that
  part's row and `attach-file`. His Send submits it.

A submission the send or the service's mail did not record: `cases.py
record-delivered --part <id> --evidence "<what shows it>"`.

#### 6. Dates

One line per case ("Form for <office>: due Fri Nov 27"). Reminders come 7
and 3 days before; the day before, the day itself and the day after are
loud. A date passing never closes a form.

#### Autonomy

| Step | Who |
|---|---|
| Yes, No | the owner only |
| Filling in from his records | ask-first; promotable (`fill_form`) |
| A signature, an attestation, a fact about himself | the owner only |
| Submitting it | his click on the form card, the service, or Send |

#### Privacy

A form's personal data (an ID number, a home address, money, health,
family) never goes in a channel post, a project room, the doc-review
repo or a shared research folder unless the owner asks for that place.
A kept case (`keep_level` local or facts) has no names or files for you:
tell him it waits for him.
