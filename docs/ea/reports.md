### Reports and other work the owner owes by a date

A report case is a document or deliverable the owner owes someone by a
date: a progress or annual report, a section of a proposal, a slide
deck, a budget. Its parts are the pieces to deliver, each with a due
date, a way to deliver it (a portal, email, unknown) and a state: asked,
accepted or declined, drafted, ready, delivered. `cases.py` keeps the
cases (kind `report`) and prints JSON. Run it as
`python3 /home/ea-bot/ea-system/preprocessor/cases.py ...`.

**The rule.** The work is the document, by its date. Never draft an email
reply to a report request on your own. A reply in the thread never
finishes a report. Only a delivery does: the sent mail that carries it,
the portal's confirmation, or the owner's word. Never put a report row's
id on a reply draft.

#### 1. Find the case

- `cases.py list --kind report`, `cases.py get --case <id>`. A ledger
  row of a case has `work_case.case` in its metadata.
- The project the report is for: `project_store.py match --text "<the
  title>"`; its pack has the earlier work (`pack --project <id>`).
- A reminder or a new part from the same sender joins the case. When
  intake is not sure, the owner answers one question.

#### 2. The first decision

"Will you do it? Yes / Yes, ask for materials / No", with the dates. The
Action requests card asks it; elsewhere, ask once.

- Yes: `cases.py accept --case <id> --quote "<their words>"`. No email.
- Yes, ask for materials: accepted, and a short request for only what
  is missing (a template, data, instructions). Name what the request
  already included. The email waits for his Send.
- No: `cases.py decline --case <id> --quote "<their words>"`. A decline
  is never yours: its email waits for his Send.

#### 3. Materials

- The template, the instructions and the data: save them in
  `~/.ea/docs/<project>/` and record each one: `cases.py materials
  --case <id> --kind template|instructions|document --ref <path>`.
- Earlier reports for the same project give the structure and the voice.

#### 4. Draft it

- Tier `draft_report` is ask-first: draft when the owner asks, or ask
  once. Promotable: `autonomy_tiers.py promote --task draft_report
  --tier do-and-tell --quote "<their words>"`.
- Draft from the materials, earlier reports and the owner's notes, in
  the template's structure. A fact, a number or a result the materials
  do not support is a question, never a sentence.
- Keep the draft in `~/.ea/docs/<project>/`. A long draft he reviews as
  a pull request in his private review repo (doc-review.md), unless the
  materials are confidential. `cases.py record-draft --case <id> --file
  <path> [--part <id>]`; on his approval, `cases.py ready --part <id>`.

#### 5. Deliver it

By the part's method:

- `email`: `create-draft` or `create-reply` with `--ledger-id` of that
  part's row and `attach-file`. His Send delivers it.
- `portal`: he uploads it, or `web_browser.py fill` and he confirms on
  its card.
- An Overleaf project (overleaf.md): commit, then stage the push for his
  button.

A delivery the send or the portal's mail did not record: `cases.py
record-delivered --part <id> --evidence "<what shows it>"`. The case
closes when every part is delivered or declined.

#### 6. Dates

One line per case ("Report for <who>: due Fri Dec 4"). Reminders come
14, 7 and 3 days before; the day before, the day itself and the day
after are loud. A date passing never closes a report.

#### Autonomy

| Step | Who |
|---|---|
| Yes, ask for materials, No | the owner only |
| A decline | never yours; its email waits for Send |
| Drafting it | ask-first; promotable (`draft_report`) |
| Delivering it | the Send button, or his click on the portal or the card |
