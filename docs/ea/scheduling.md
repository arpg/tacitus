## Calendars and scheduling

The user's calendars are listed in `~/.ea/config/scheduling-preferences.json`.
Calendar math (free slots, conflict detection, preference enforcement)
is handled by `scheduling_engine.py`. NEVER compute availability yourself.

`scheduling_engine.py` merges Outlook + both Google calendars, applies
working hours, lunch blocks, buffers, and no-meeting days, and returns
both free slots and full event context. Use it for ALL scheduling
queries. It has exactly five subcommands: `propose-meeting` (open "what
times work?"), `validate-draft` (is this exact slot free on the user's
own calendars? It reads no one else's),
`check-attendees` (are these people free in this window? It returns
each person's free/busy and the user's own events in the window with
their ids), `log-decision` (record that the user moved, kept or
cancelled a meeting), `review-patterns` (suggest preference updates
from those logged decisions). `reschedule-brief` wraps the first two
for a "can we move it?" email. The first three take `--ignore-event
<id>` (repeatable) to treat one of the user's own events as free for
that call, which is how "can Uma join my existing 1:1" gets answered.

**Booking links.** A cal.com link someone sends is read with
`booking_link.py`, which only reads: `propose` runs `propose-meeting`
with each link's open slots as that person's availability. Never book
through the link. The meeting is the user's own invite
(`pending-event.json`, Create button). Mode C in
`/home/ea-bot/ea-system/skills/scheduling-rules.md` has the workflow.

**Someone in another time zone.** Put `"timezone":"Europe/London"` on
that attendee in `propose-meeting` and the solver keeps every option
inside their working hours (9:00 to 17:00 local; `"hours":["10:00","16:00"]`
narrows it). `"excluded_days":["YYYY-MM-DD"]` on that attendee blocks
the days they ruled out and composes with their zone's hours; stated
`availability` replaces the hours instead. Quote the option on the
user's clock (the solver's `label`, with the zone's own short name) and
add their local time.
An open proposal returns separate windows; offered times (an attendee
with `availability`) and a poll reply's `--num-options 999` list every
feasible quarter-hour start inside the window.

**A draft the user wrote.** `search-mail` and `list-mail` rows carry
`is_draft` and `own`. "Finish the draft I wrote" means read that row with
`get-message`, keep the user's words, and `update-draft` it; never
`create-reply` beside it.

All-day event judgment (block vs informational) IS your job.
`propose-meeting` and `validate-draft` both return an `all_day_events`
list. Each entry carries the engine's `classification` (hard_block,
soft_penalty, unknown, ignore) and the `effect` it applied (blocked,
penalized, none). Conference/travel = block, reminder/task = ignore.
Overrule the engine on anything it classified `unknown`. Run
`log-decision` after the user acts on a meeting; that is what
`review-patterns` reads.

Read `/home/ea-bot/ea-system/skills/scheduling-rules.md` for the full scheduling workflow
and all defaults. That file is the single source of truth for
scheduling behavior. Read `~/.ea/config/scheduling-preferences.json`
for per-contact overrides and VIP list.

**Finding an Outlook event id:** `graph_actions.py calendar-view --start
<ISO> --end <ISO>` (or `--days N`) lists every Outlook event in the range
with its `id`, subject, times, attendee count, `event_type`
(singleInstance / occurrence / seriesMaster), `show_as`, `categories` and
`ea_hold`. An occurrence's id moves that occurrence only. This is the
step before any reschedule or delete; the ids are not in the briefing
or the Google mirrors.

**Calendar reschedule/delete workflow:** When the user asks to move or
cancel a calendar event:
1. Use `get-event --event-id <id>` to retrieve current event details
2. For reschedule: write `~/.ea/state/pending-event-update.json` with
   `{"event_id": "<id>", "subject": "...", "start": "ISO", "end": "ISO",
   "attendees": "email1,email2", "optional_attendees": "email3",
   "location": "..."}`. Anyone already optional on the event stays
   optional; `optional_attendees` makes more people optional. `subject`
   renames the event: give the event's own title, or leave it out, to
   keep it. The card shows the event as the calendar has it, and the
   change as a difference.
   Post a short message: ":calendar: Rescheduling [event] to [new time]."
3. For deletion: write `~/.ea/state/pending-event-delete.json` with
   `{"event_id": "<id>", "subject": "...", "start": "ISO", "end": "ISO"}`.
   Post a short message: ":calendar: Deleting [event]."
4. The bot reads these files and posts `:lock:` confirmation blocks with
   Reschedule/Delete buttons. Do NOT call `update-event` or `delete-event`
   directly — the bot handles it after user clicks the button.
For any event creation (with OR without attendees): write
`~/.ea/state/pending-event.json` with `{"subject", "start", "end",
"attendees", "optional_attendees", "location", "body", "online"}`. The
`attendees` field may be an empty string for personal holds.
`optional_attendees` (same format, may be omitted) are invited as Outlook
optional attendees: use it whenever the user says someone is optional,
and do not write "this one is optional for you" into the body instead.
The Create card shows them on their own line. The bot posts a `:lock:` Create-button
confirmation; do NOT call `create-event` directly. An Outlook event with no
attendees and no optional attendees gets no card: the bot creates it when
you stage it and posts one line in the thread naming what it created (at
most 30 an hour; past that, cards again). A Google calendar event always
gets the card. Add a one-line chat
preface like ":calendar: Holding Tue 9-10am for focus time." before the
bot's confirmation block.

**Google calendars** (the family calendar or the personal one): add `"calendar": "<account>"` to the event
object. The Create button then makes it on that Google calendar instead
of Outlook. Any address in `calendar_accounts` of `user-profile.json`
works, or a key from `scheduling-preferences.json` such as
`google_family`; `"outlook"` or no key is Outlook. A Google event
invites no one: leave `attendees` empty (the bot refuses the card
otherwise), and `online` and `hold` are ignored. Repeat keys work the
same. To put one event on BOTH calendars, stage a JSON array with an
Outlook object and a Google object; each gets its own Create button.
Moving or deleting a Google event works the same way: put `"calendar":
"<account>"` in `pending-event-update.json` or `pending-event-delete.json`
beside the Google `event_id`, and the Reschedule or Delete button acts
on that calendar. Google ids come from
`google_calendar_client.py get-events --email <account> --start <ISO> --end <ISO>`:
`id` is one occurrence, `recurring_event_id` is the whole series. A
series id moves or deletes every occurrence; an occurrence id only that
one. A move changes the time, title or location; to change a series'
days, delete it and stage a new one.

**Holds on Outlook:** add `"hold": true` to a pending event with no
attendees. It is created shown as tentative and tagged "EA Hold", and you
may release it later yourself with
`python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py clear-hold --event-id <id>`
(no button; the verb refuses any event that is not a tagged, tentative,
attendee-less hold of the user's own). Use it for a slot parked while a
counterpart decides, and clear it when the meeting lands elsewhere or is
dropped. Holds for your own solver work go on the EA-Managed Google
calendar instead (`google_calendar_client.py`), where `delete-event
--event-id <id>` needs no calendar id and no button.

Every staged file (`pending-event.json`, `pending-event-update.json`,
`pending-event-delete.json`, `pending-schedule-send.json`) also honours
two OPTIONAL routing keys: `"thread_ts"` and `"channel"`. Include the
`thread_ts` from your dispatch marker so the buttons land in the thread
that asked for them; without it they go to the channel root.

**Repeating events** (a semester of lectures, a weekly standing hold):
add the recurrence keys to the SAME event object and Outlook gets ONE
series instead of N events: `"repeat": "weekly"|"daily"`, `"repeat_days":
"TU,TH"` (weekly only), `"repeat_until": "YYYY-MM-DD"` or
`"repeat_count": N`, optional `"repeat_interval": N` (every N weeks/days).
`start`/`end` then describe ONE occurrence, so keep them on the same day.
A series still needs one Create button, not one per occurrence.

**Multiple events** (multi-day programs, unrelated events in one pass):
write `pending-event.json` ONCE as a JSON ARRAY of event objects. The bot
posts ALL the Create buttons together in one pass. NEVER do
one-event-per-round-trip — that forces the user to confirm, wait, and
re-prompt for each event. Each event still gets its own button; that
per-event gate is by design. Use the array for events that genuinely
differ; a fixed weekly pattern belongs in one repeating event.
