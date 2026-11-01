### Usage tips (one new thing to try, once a week)

Most people use the EA as a chat box and never meet most of what it
does. Many never need it. `preprocessor/usage_tips.py` notices what the
owner has not used yet and, once a week, suggests one thing in plain
words with one line to try. It never suggests a thing the owner uses
already.

**The tips, in order.** Ask for a reply draft; ask what is on tomorrow;
snooze an email; state a standing rule; turn on phone notifications;
pair a phone for use away from the home network; ask for a meeting
time; fill a poll from an email; attach a file to a request; mark
things done from the Needs you list; give a project its own room (only
after 20 conversations); choose which jobs run on the local model in
Settings (only when a local model is set up). The phone, Needs you and
Settings tips need the web chat. `usage_tips.py list` prints the
catalogue with each tip's state.

**How the EA knows what is used.** Each tip has a detector that reads
what the bot keeps already, read-only: the web chat's store (the
owner's messages, the conversations they started, cards acted on,
rooms, uploads, notification subscriptions, Mark done overrides), the
relay's paired phones, the draft journal, the ledger and the snooze
file, the preferences store, the uploads folder, the room registry and
the routing table. A store this process cannot read counts as "not
yet".

**When it posts.** The morning briefing's hourly weekday cron tick runs
`usage_tips.py post` (run-agent.sh), which posts only when all of these
hold: 7 days have passed since the first briefing (the onboarding
questions come first); 7 days have passed since the last tip; it is a
weekday between 07:00 and 12:00 local; that day's briefing has posted;
the briefing carried no onboarding question that day; it is not quiet
hours. A new tip closes the one before it, so one tip is open at a
time. The post goes through `chat_notify` to the EA channel, its own
thread, with a push to the web chat's phones at normal urgency.

**The owner's answer.** In the tip's own thread the chat bot answers
these words itself, with no model call: *more* posts the next tip there
now; *not now* pauses the tips for 14 days (that tip comes back, twice
at most); *never* or *stop* ends them and records a rule in the
preferences store (`usage-tips-stop`, kind `instruction`, scope
`briefing`). Any other message in that thread is a normal turn.

**Seeing, resetting, turning off.** `usage_tips.py status` shows each
tip's state and why the next one may or may not post now; `next` is the
same as a dry run. `reset` forgets every tip shown and answered (state
in `~/.ea/state/usage-tips.json`). Retiring the `usage-tips-stop` rule
(`preferences.py retire --id usage-tips-stop`) turns the tips back on.
`"usage_tips": {"enabled": false}` in `~/.ea/config/ea-config.json`
turns them off for the install.
