### First-week onboarding (one question a day)

Setup fills the config once, and `preferences.json` starts empty, so
the first briefings and drafts are generic. `preprocessor/onboarding.py`
closes that gap: it asks the question bank in
`config/onboarding-questions.json` and records each answer as a
standing rule in the user's own words.

**What it asks.** Thirteen questions, one sentence each, with a reason
and an example answer: the senders never to bury, the lists to skip,
what counts as urgent, what the EA must never do on its own, meeting
limits beyond working hours and protected days, when not to interrupt,
the sign-off and tone for drafts, who never gets a draft without asking,
standing facts (roles, grants, eligibility), briefing length, funding
never worth showing, and reviewing requests. Each question names the
`scope` and `kind` its answer becomes. The bank carries no institution
or personal names; the user supplies those.

**How it is asked.**
- Chat profile: the preprocessor runs `onboarding.py next` for the
  morning briefing, which ends with the question of the day under
  `:seedling: *One question*`. The user answers in that thread; the
  turn runs `onboarding.py answer --id <id> --text "<their words>"` and
  confirms the rule in one line. *skip* runs `skip --id <id>`, *stop
  asking* runs `stop`. The afternoon catchup never asks; it only
  records an answer. One question a day: `next` returns nothing while
  the question asked today is still open (`--force` overrides).
- Email profile: the whole bank in one sitting, either
  `python3 scripts/install/user_init.py --phase preferences` (interactive) or
  `python3 preprocessor/onboarding.py ask --count 3`, which prints the
  questions with the exact `answer` and `skip` commands. The setup
  skill and the doctor point at `ask`.

**Where the answers go.** A rule in `~/.ea/config/preferences.json`,
id `onboarding-<question id>`, source `onboarding`, text
`<topic>: <the answer>`, quote the answer verbatim, kind and scope from
the bank. From then on it is injected into every prompt whose skill
declares that scope and outranks the skill text, like any other rule.
A second answer to the same question retires the first rule. The
bookkeeping (asked, answered, skipped, done) is
`~/.ea/state/onboarding.json`, written through `resident_io`.

**Seeing and retiring.** `python3 preprocessor/onboarding.py status`
lists answered, skipped and remaining ids; `preferences.py list` shows
the rules; `preferences.py retire --id onboarding-<id> --note "..."`
withdraws one. Answering the question again replaces it.

**Turning it off.** `onboarding.py stop` marks the bank done (what the
user's "stop asking" runs). To disable it for an install, set
`"onboarding": {"enabled": false}` in `~/.ea/config/ea-config.json`;
`next` and `ask` then return nothing and the briefing carries no
question. The questions stop on their own once every one is answered
or skipped.
