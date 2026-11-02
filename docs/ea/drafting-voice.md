### Drafting in the user's voice

These rules bind every writer: the remote writer (`email_writer.py`,
which is given this guide), the `email-author` subagent, and a lane
that writes a body itself.

The briefing JSON includes a `writing_style` section extracted from the user's
actual sent mail. Use this to match their real voice:

- **Per-contact profiles (AUTHORITATIVE)**: before drafting to anyone, run
  `python3 /home/ea-bot/ea-system/preprocessor/style_profiles.py get --contact <addr>`.
  This is a persistent database accumulated from ALL sent mail (not just the
  recent window) and includes the user's most recent real emails to that
  person. MIMIC those examples: same formality, same length class, same
  sign-off.
- **Per-contact data** (`writing_style.per_contact`): recent-window fallback
  if the profile DB is unavailable.
- **Defaults** (`writing_style.default_*`): For contacts with no history
  (`found: false`), use the user's overall default greeting and closing.
- **Length**: Match the user's typical email length for that contact. If they
  write 60-word emails to someone, do not write 200-word drafts.
- **Contractions**: Check `writing_style.uses_contractions`: if true, use
  natural contractions (don't, can't, I'll). If false, write them out.
- **Never use em-dashes** in any drafted content.
- **State the fact and stop.** Do NOT close a paragraph with a clause that
  editorializes about significance or tells the reader what to conclude.
  A person rarely sells a point after making it, and a significance
  clause is the clearest tell that a model wrote the text. Delete endings
  like "which is why this matters so much" or "that is a real advance, not
  an incremental step". After drafting, reread each paragraph's last
  sentence: if it restates the point at a higher level, claims importance,
  or explains why the reader should care, cut it. The sentence before it
  is the right ending.
- **Avoid "X is Y, not Z"** and its "rather than" / "instead of" variants.
  Rephrase as a direct positive statement. Exception: a genuine functional
  contrast ("book the Portland hotel rather than the Tucson one")
  is fine, since it instructs rather than editorializes.
- **Strip non-technical adverbs**: directly, exactly, precisely, simply,
  genuinely, deliberately, squarely, notably, crucially, importantly,
  clearly, actually, truly, ultimately. Keep adverbs doing real semantic
  work ("internationally recognized", "spatially explicit").

### Voice calibration (from the user's own edits)

Most people rewrite drafts along consistent lines. The edits below are
common ones; the user's own edits (`style_analysis.py`, the
`drafting` rules in preferences.json) say which of them apply. Anticipate
them instead of making the user fix them:

- **Hedge judgments.** Many writers add "as I see it", "IMO", "I think" to
  claims a draft states flatly ("The problem is capacity" becomes "The
  problem as I see it is capacity"). Facts stay flat; opinions get marked
  as opinions.
- **Say you do not follow, rather than that they are wrong.** "The gap is
  smaller than it looks" becomes "I don't follow the gap argument."
  Naming one's own confusion is a gentle way to push back.
- **Keep one admission of what the user does not know**, e.g. "I have
  limited contact with the lab's hiring process." A second one weakens
  the email, so spend it where it buys the most.
- **Cut the closing argument.** Summary paragraphs, verdicts and
  rhetorical last lines ("Taking this on would spend the one advantage we
  have") usually come out. A long email ends with an invitation or an open
  question, often a request to talk in real time.
- **Name a concrete local thing instead of citing a principle.** "The
  outcome we ruled out at the spring planning meeting" becomes "the
  outcome of Gale's intro class, IMO." Point at the actual course,
  person or meeting.
- **"We" for group action.** "I will rename ABCD 4000" becomes "we
  can rename ABCD 4000."
- **Keep the user's asides and exclamation marks.** Parentheticals like
  "(again, the new equipment is critical here.)" and sentences ending in
  "!" can be part of a register with colleagues. Match what the profile
  shows; do not smooth them out.
- **Operational detail in, rhetorical detail out.** Specifics that
  constrain a decision ("not even the new program", "unclear next year
  too") stay; specifics that only score a point go.
- **Corrections point at the expert.** Asked to walk back a claim, one
  line is enough: "I may have spoken too soon. Fern knows the
  data-services side better than I do and can clarify." No
  self-criticism, no re-argument.
- **Drop what the recipient already knows.** Thread context is the first
  thing to cut.
- **Length by type.** Short transactional replies (scheduling, thanks) are
  20 to 70 words. Long argumentative emails lose argument before they lose
  evidence.

### Measured from sent mail

`style_analysis.py` measures the user's sent mail each month: sign-off
habits, greetings, length, and hedge and exclamation rates per audience
segment. Quoted history, disclaimers and meeting-invite boilerplate are
excluded. Those figures are the target; where they disagree with a hunch
about a contact, they win. Per-segment rates rest on small counts, so
treat the ordering as the signal and the second decimal as noise.

What to look for in the measurements, with made-up example figures:

- **Sign-off.** Many writers end most emails with the name alone ("74% end
  with the name alone; `Best` is the only common closing word"). If that
  is the user's habit, do not append `Best,` by reflex.
- **Greeting.** One greeting usually dominates (`Hi {name},`); a formal one
  (`Dear`) may be reserved for external or first contact.
- **Default to short.** A typical median is a few dozen words ("median 40
  words; 75% under 80"). A draft far above the user's 90th percentile
  needs a reason.
- **Hedge density tracks the audience.** Peers often get the most marked
  judgments; a factual report to a sponsor gets the fewest.
- **Exclamation marks are collegial.** They cluster in mail to students
  and close colleagues; keep them out of contentious threads.
- **Long mail goes to leadership and sponsors**, short mail to students
  and quick logistics.
- **Dashes.** The em-dash rule holds whatever the corpus shows. Do not
  write an em-dash or a spaced double hyphen, and do not claim in the
  user's voice that they never use one.

Before drafting, run `style_profiles.py get --contact <addr>`. It
returns `sign_off`, `median_words`, `p90_words`, and per-1k question,
exclamation and hedge rates for that person. Those are the target.

- In chat: casual, use emoji sparingly for scanability, keep it short.
- **Emoji**: Use shortcodes (`:sunny:`, `:email:`, `:calendar:`,
  `:warning:`, `:white_check_mark:`, `:alarm_clock:`, `:memo:`, `:pushpin:`,
  `:red_circle:`, `:arrow_right:`) instead of Unicode emoji characters in
  chat messages. Unicode emoji may render incorrectly via the API.

If `writing_style` is not available (chat bot context without briefing),
fall back to: direct, concise, match the sender's formality level.

## Pre-send review (executive functioning check)

Before calling `send-draft`, perform a two-layer review:

### Layer 1: Programmatic preflight
Run `python3 /home/ea-bot/ea-system/preprocessor/mail_actions.py preflight-check --provider outlook|gmail --draft-id <draft-id>`.
This checks for: empty body, missing recipients, a meeting link from
another video platform in the body, missing quoted thread on replies. If
it returns errors, fix them before sending. Warnings should be reviewed
but don't block.

### Layer 2: LLM self-review
Before sending, mentally verify this checklist:
- **Name check**: Does every name in the draft match the From/To fields
  or the obligations ledger exactly? No guessed last names?
- **Content check**: Does the draft actually answer what was asked?
  If it's a scheduling reply, are the proposed times from the engine
  output (not invented)?
- **Meeting-yes check**: A reply that agrees to meet, to a call, to
  touch base or to catch up carries two or three times from
  `scheduling_engine.py propose-meeting` (`docs/ea/scheduling.md`), in
  the user's time zone, with the other person's local time when they are
  elsewhere. A yes without times is an unfinished reply. The two
  exceptions: the user said to leave times out, or the other side's
  assistant or coordinator is finding the time.
- **Tone check**: Does the draft match the user's writing style for
  this contact? Right length, right formality?
- **Attachment check**: If the original email referenced attachments
  or the reply promises an attachment, is one attached?
- **Meeting details check**: If creating a calendar event, does it use
  the user's video platform (`institution.video`) and no other? Correct
  duration? Correct attendees?

If any check fails, fix the issue and re-run preflight before sending.
The `send-draft` command also runs preflight automatically and will
block if errors are found.
