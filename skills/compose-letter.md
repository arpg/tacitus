# Skill: Compose Letter
<!-- scopes: drafting -->

Generate a formal letter on the institution's letterhead using LaTeX, compile
to PDF, and optionally attach to an Outlook draft.

The user may provide content in several ways:
- Polished prose → use as-is, just format into the template
- Rough dictation ("dictation starts: ...") → clean up grammar, organize
  into paragraphs, but preserve the user's voice and specific claims
- Bullet points / key themes → expand into full letter prose
- Just a name and purpose ("write a rec for Student X for Y") → draft
  from scratch using context from email, CV, and prior correspondence

## Context available to you

- **User profile**: `~/.ea/config/user-profile.json` — the user's name, role,
  department, and bio blurb. Read this first.
- **Letterhead directory** (`<letterhead>` below): the profile's
  `institution.letterhead.template_dir` (default
  `~/.ea/templates/letterhead`). The letterhead style and logos may sit
  in one of `institution.letterhead.asset_dirs` instead (an institution
  plugin ships them there); `compile_letter.py` finds them either way.
- **Template**: `<letterhead>/template.tex`, the LaTeX skeleton.
- **Example letter**: `<letterhead>/example-recommendation.tex`:
  a real recommendation letter showing the user's writing style: direct,
  substantive, specific-examples-over-generic-praise, 1 page, with a
  nuanced evaluation (not just cheerleading). Study this carefully and
  match the voice.

## Letter types

- **Recommendation** — for students, postdocs, colleagues applying to jobs,
  programs, awards. The user knows the person; use specific technical
  observations, not hollow praise.
- **Letter of interest / cover letter** — for programs, fellowships,
  affiliations the user is applying to. Emphasize relevant expertise.
- **Letter of support** — for proposals, collaborations, grants where the
  user endorses someone else's work or commits resources.
- **Nomination** — for awards, positions, honors.
- **General formal**: anything else on the letterhead.

## Steps

### 1. Parse the request
Extract from the user's message:
- **Type** (rec, interest, support, nomination, general)
- **Recipient** (person, committee, organization)
- **Subject** (who/what is being recommended/applied for)
- **User's content** — everything after "dictation starts:", or the
  bullet points, or the prose. This is the core material.
- **Related email** — if the user says "to respond to this email" or
  references a thread, search for it.

### 2. Gather source material
Do these in parallel where possible:

a) **Read the user profile** (`~/.ea/config/user-profile.json`) for bio and role context.

b) **Read the example letter** at `<letterhead>/example-recommendation.tex`
to calibrate voice and structure.

c) **Search email** (if relevant): look up the subject person or program
in Outlook to get context, the original request, deadlines, etc.
```bash
python3 ~/ea-system/preprocessor/mail_actions.py search --provider outlook --query "<person or program>"
```

d) **Read the template**: `<letterhead>/template.tex`

### 3. Write the letter

**If the user dictated content**: clean up the dictation into polished
prose. Preserve their specific claims, examples, and assessments. Fix
grammar, organize into paragraphs, add transitions. Do NOT add claims
the user didn't make — if they said "she's good at X", write about X,
don't invent Y and Z.

**If the user gave bullet points**: expand each into a paragraph with
supporting detail drawn from context (email threads, CV knowledge).

**If the user gave just a name and purpose**: draft from scratch. Pull
from email correspondence with the person, the user's knowledge of their
work, and relevant program details. Flag any claims you're unsure about
with [VERIFY] so the user can check.

**Style (match the example letter):**
- First person, direct, authoritative
- Open with relationship to the subject and context
- Body: specific technical observations, concrete examples
- For recs: honest assessment including nuance ("while X, she demonstrates Y")
- Close with clear endorsement and forward-looking statement
- Keep to 1 page unless explicitly told otherwise
- LaTeX special chars must be escaped: & → \& , % → \%, $ → \$, # → \#, _ → \_

### 4. Write the .tex file

Replace the template placeholders:
- `<<RECIPIENT_BLOCK>>`: name/title/org, or `$\quad$` if generic/unknown
- `<<SALUTATION>>`: "Dear [name/committee],"
- `<<BODY>>`: the letter paragraphs
- `<<CLOSING>>`: "Sincerely," (default)

Save to `~/.ea/letters/<slug>.tex` (e.g., `fellowship-interest-2026.tex`,
`studentname-rec-programname.tex`).

### 5. Compile to PDF
```bash
python3 ~/ea-system/preprocessor/compile_letter.py \
  --tex ~/.ea/letters/<slug>.tex \
  --output ~/.ea/letters/<slug>.pdf
```
If pdflatex fails, fix LaTeX errors (usually unescaped special chars)
and retry. Do not give up — fix and recompile.

### 6. Present to user
Show in chat:
- The full letter text in readable form (not raw LaTeX)
- Confirm PDF compiled: "PDF ready at `~/.ea/letters/<slug>.pdf`"
- Ask: "Want me to create an Outlook draft to send this? Or edit first?"

### 7. On approval
If the user says "send" or "email it":
- Search for the relevant email thread to reply to, OR create a new draft
- Create Outlook draft via mail_actions.py create-draft --provider outlook
- Tell the user to attach the PDF from `~/.ea/letters/<slug>.pdf`
  (file attachment requires manual step with current Outlook REST token)

### 8. Update state
- If this was a tracked obligation (LoR request etc.), mark it progressed/completed
