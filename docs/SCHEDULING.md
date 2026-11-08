# Scheduling System

The EA scheduling system proposes meeting times by merging multiple
calendars, applying preference constraints, and solving for optimal
slots using constraint programming.

## Overview

The `propose-meeting` command in `scheduling_engine.py` is the single
entry point for all scheduling. It:

1. Reads the user's merged calendar (Outlook + Google accounts)
2. Checks internal attendees' availability via Microsoft Graph API
3. Accepts stated availability windows for external attendees
4. Applies hard and soft constraints read live from tokened events on
   the EA-Managed calendar (numeric weights come from
   `scheduling-preferences.json`)
5. Solves for the best slots using Google OR-Tools CP-SAT
6. Returns ranked options and conditional options (e.g., movable 1:1s)

```bash
python3 preprocessor/scheduling_engine.py propose-meeting \
  --duration 30 --days-ahead 7 --num-options 5 \
  --attendees '[{"email":"person@example.edu","name":"Name","type":"internal"}]'
```

## Preferences model

Scheduling truth lives in three layers. The first two are calendars the
user can see and drag; only numeric tuning stays in config.

**Layer 1, busy.** Every real meeting on Outlook and the blocking
Google calendars hard-blocks its slot through the merged busy view. A
meeting with two or more attendees is immovable by construction and
needs no declaration anywhere. This is why the EA-Managed calendar does
not carry a copy of every meeting: a mirror of an ordinary meeting would
change nothing the solver does.

**Layer 2, rules.** Day/time policy lives as tokened, usually
recurring events on the EA-Managed calendar. The leading word of the
title is the token; the rest is a human label the solver echoes back in
its output:

| token | meaning | effect |
|-------|---------|--------|
| `wh`  | working hours | slots proposed ONLY inside these windows |
| `nm`  | no meetings | hard block (lunch, no-meeting Friday) |
| `p0`  | hard constraint | hard block |
| `p1`  | strong preference | large soft penalty (`p1` weight) |
| `p2`  | mild preference | small soft penalty (`p2` weight) |
| `wr`  | working remotely | informational only, never blocks |

There is **no config fallback**: if the horizon has no `wh` window,
`propose-meeting` returns an error rather than guessing hours. The
solver's output carries `calendar_preferences.tokens` (exactly what was
read, with every token key present so an empty list means "that rule
does not exist right now"), `excluded` (what was dropped and by which
token), and per-option `applied_preferences`. Weekends are hard-blocked
regardless of tokens.

**Layer 3, movability declarations (`[auto]` mirrors).** The calendar
protection reconciler (`preprocessor/calendar_reconciler.py`, cron every
30 minutes) reflects EVERY timed commitment on the real calendars as an
`[auto]` event on EA-Managed: a series as ONE recurring mirror anchored
to the source series id, a one-off meeting as one single mirror anchored
to its event id, and a series whose pattern has no RRULE form (monthly,
yearly) occurrence by occurrence. The rule name is written into the
event description. The token level is the movability policy, defaulted
from the meeting's shape and thereafter owned by the user:

| source shape | default mirror | meaning |
|---|---|---|
| a level on the source title (`p0 ...`, `[note]`) | that level | the user's own label; wins over every default |
| an event on the family calendar | `[note] [auto]` | visible, never blocks |
| a mirror the user deleted | `[note] [auto]` | released; deleting the note too ends it |
| invite not accepted (tentative, no response) | `p2 [auto]` | on the calendar, not yet a commitment |
| zero other people | `p0 [auto]` | personal commitment (lecture, visit, hold); never offered away |
| 1:1 with someone inside the institution | `p1 [auto]` | offerable in relaxation and as a poll overbook, ranked down |
| 1:1 with someone outside it | `p0 [auto]` | a moved outside meeting takes weeks to land |
| group meeting | `p0 [auto]` | stands; nobody offers away a department meeting |

"Other people" leaves out the user and rooms; a title that names a 1:1
("1:1", "1-on-1", "A <> B", "Sam / Pat") counts as one even when the
other person appears under two addresses. A one-off already covered by a
hand-placed token of the same or a higher level gets no mirror, and a
one-off mirror stays after its meeting ends; a one-off moved or
cancelled while still ahead retimes or removes its mirror without a
chat post.

Editing the token word on a mirror changes the policy (promote a `p1`
1:1 to `p0` and it becomes untouchable); the reconciler preserves level
edits and manages times only. Lifecycle follows the source: a moved
series retimes the mirror, a deleted series removes it, and a single
cancelled source instance (a lecture skipped for a holiday) cancels the
matching mirror occurrence. The USER deleting a mirror is a decision:
logged to `scheduling-decisions.json` as a release and never recreated.
The reconciler never touches events lacking the `[auto]` marker, and a
failed source fetch freezes that source's mirrors instead of removing
them.

The solver reads the mirror level in place of the raw event
(`_build_busy_intervals`, `_mirror_level`): `p0` blocks and is never
offered away, `p1` blocks but the relaxation pass may offer it, `p2`
does not block (its token adds the p2 penalty) and `[note]` is free
time. An option that sits on a `p2` or `[note]` event says "Overlaps X".
Events find their mirror by the reconciler's source key (Exchange series
or event id, Google calendar and series or event id); a Google copy of
an Exchange series finds the Exchange mirror by its identical
occurrence. An event with no mirror yet, made since the last reconciler
run, keeps the exactly-one-attendee heuristic below. Beyond solver ranking, the
declarations drive poll filling (`skills/fill-poll.md`): `p0` overlaps
are never offered in a Doodle/When2Meet response, `p1` overlaps are the
overbook candidates, which is what makes high-stakes polls (deans,
defenses, sponsors) answerable with maximum honest availability.

Remaining soft signals with no calendar token: clustering (prefer slots
near existing meetings; there is no separate back-to-back penalty) and
a slight preference for earlier days in the window. Both are weights,
not time rules (see the weight reference below).

### Relaxation passes

When the primary pass finds fewer options than requested, the solver
runs a relaxation pass:

- **movable_1on1**: Treats 1:1 meetings as soft conflicts. A 1:1 is an
  event with EXACTLY one attendee besides the organizer
  (`_is_movable_block`). Zero attendees is a personal block and is
  never offered away; non-token EA-Managed holds are the exception,
  since holds are reclaimable by design. These appear as
  `conditional_options` with a label like "Move 1:1 with Jordan
  (Tue 10:00) to accommodate."

Conditional options are presented separately so the user can decide
whether to move the conflicting meeting. A slot covered by any `p0`
token, including an `[auto]` mirror, survives relaxation: it is a hard
block in both passes.

### All-day event handling

All-day events are classified by the solver using keywords:

- **Hard block**: "conference", "travel", "vacation", "out of office"
- **Soft penalty**: "hold", "tentative", "placeholder"
- **Ignored**: "deadline", "reminder", "birthday", "task"

The LLM makes the final judgment call on ambiguous all-day events.

### Informational events (`[note]`)

Some calendar entries record WHEN something happens without committing
the user's time: the house cleaner, a delivery window, someone else's
travel. Any event whose title contains `[note]` is dropped from the busy
intervals the solver builds. An all-day `[note]` is also dropped from the
list handed to the LLM for the block-or-ignore judgment above.

The marker works on ANY calendar and matches anywhere in the title,
case-insensitively, so `[note] house cleaner` and `house cleaner [note]`
both work and an existing event can be tagged in place without retyping
its title. That is what separates it from the preference tokens (`wh`,
`nm`, `p0`, `p1`, `p2`, `wr`), which must lead the title and only carry
meaning on the EA-Managed calendar. See `skills/scheduling-rules.md` for
the token vocabulary.

Implemented as `_is_note_event()` in `scheduling_engine.py`, applied in
`_build_busy_intervals()` and `_get_all_day_events_on_date()`.

## Contact resolution

Attendee email addresses are resolved via Microsoft Graph People API
(`resolve-contact` command). The solver uses:

- **Internal attendees** (`type: "internal"`): the user's own domain (`@example.edu` here)
  addresses. Their free/busy is checked via Graph API `getSchedule`.
- **External attendees** (`type: "external"`): Provide `availability`
  windows parsed from their email (e.g., "I'm free Tuesday 2-5 PM").
  The solver intersects these with the organizer's availability.

## Solver behavior

The CP-SAT solver:

1. Builds a grid of 15-minute slot indices across the search window
2. Hard-blocks slots that violate hard constraints
3. Assigns soft costs to remaining slots based on preferences
4. Minimizes total weighted cost to find optimal slots
5. Returns up to `num_options` ranked results
6. If fewer than `num_options` found, runs relaxation passes

Calendar sources checked:
- Outlook (via Graph API, in preprocessor)
- Google Calendar primary (via Google Calendar API)
- Google Calendar secondary (optional, configurable)

## Preference tuning guide

Day/time rules are calendar events, and changing one is a calendar
operation: drag or edit the tokened event on the EA-Managed calendar in
any Google Calendar UI, and the solver reflects it on its next run. The
former config keys (`working_hours`, `preferred_hours`,
`no_meeting_blocks`, `no_meeting_days`) were deleted on 2026-07-17 and
have no fallback; if they are absent from
`scheduling-preferences.json`, that is correct.

### Change working hours

Drag the recurring `wh Working Hours` event (e.g. `wh` 9:00-17:00
Mon-Thu). Windows can differ per day: a separate shorter `wh Friday
working hours` event gives Friday its own hours. Deleting all `wh`
events for a period makes `propose-meeting` return an error for that
period, so a standing `wh` series should always exist.

### Change preferred hours

Create or drag a `p2` event over the hours to DISCOURAGE, e.g.
`p2 Prefer mornings` covering 15:00-17:00. Slots inside it still appear
but rank lower. Use `p1` for a strong preference. Delete the event to
disable the preference.

### Add or remove lunch blocks and no-meeting days

Both are `nm` events: a recurring `nm Lunch` 12:00-13:00, or an all-day
recurring `nm No-meeting Friday`. Delete the event to reopen the time.

### Protect or release a standing commitment

Recurring attendee-less busy events are mirrored automatically as
`p0 [auto]` holds by the reconciler. To release one, delete the mirror
on the EA-Managed calendar: the release is logged and permanent. To
protect something the rule does not catch (e.g. a specific recurring
1:1 that must never be offered as movable), create a hand-placed `p0`
event over it; events without `[auto]` are yours and the reconciler
never touches them. Tag an event `[note]` anywhere in its title to mark
it informational: it stops blocking and stops being surfaced for
all-day judgment.

After manual UI edits, `google_calendar_client.py recolor-tokens`
restores the token color convention (green `wh`, red `nm`/`p0`, amber
`p1`/`p2`, graphite `wr`).

### Adjust solver weights

Add a `solver_weights` key to override any default weight:

```json
"solver_weights": {
  "lunch_overlap": 5.0,
  "after_preferred": 1.0
}
```

Higher values make the solver avoid those slots more strongly. A value
of 0 disables the penalty entirely.

## Weight reference

| Weight | Default | What it penalizes |
|--------|---------|-------------------|
| `after_preferred` | 0.5 | Slots at or after `preferred_hours.end` |
| `lunch_overlap` | 3.0 | Slots overlapping a lunch block |
| `preferred_slot_miss` | 2.0 | Outside a stated preference from `--constraints` |
| `preferred_window_miss` | 1.5 | Outside a preferred window constraint |
| `clustering` | 1.0 | Far from other meetings (breaks focus blocks) |
| `later_in_week` | 0.5 | Later days in the search window |
| `all_day_hold_tentative` | 2.0 | Days with tentative/hold all-day events |
| `all_day_unknown` | 1.0 | Days with unclassified all-day events |

## Common scenarios

### "Find a time to meet with X next week"

```bash
python3 preprocessor/scheduling_engine.py propose-meeting \
  --duration 30 --days-ahead 7 --num-options 5 \
  --attendees '[{"email":"x@example.edu","name":"X","type":"internal"}]'
```

The solver checks both calendars, applies all preferences, and returns
5 ranked options. Morning slots rank highest (before 3 PM), afternoon
slots (3-5 PM) appear but rank lower.

### "When can I meet with an external collaborator who is free Tue/Thu afternoons?"

```bash
python3 preprocessor/scheduling_engine.py propose-meeting \
  --duration 60 --days-ahead 14 --num-options 5 \
  --attendees '[{"email":"collaborator@example.edu","name":"Collaborator","type":"external",
    "availability":[
      {"day":"2026-04-21","start":"13:00","end":"17:00"},
      {"day":"2026-04-23","start":"13:00","end":"17:00"}
    ]}]'
```

The solver intersects the external availability with the organizer's
free time and preferences.

### "Fill a When2Meet poll"

Treat the poll's time grid as an external attendee's availability.
The solver returns which poll slots are free, conflicted, or
conditionally available (movable 1:1s).

### "I need to meet with someone but my week is packed"

If the primary pass finds few options, the solver automatically tries
relaxation (movable 1:1s). Conditional options appear with labels like
"Move 1:1 with Jordan (Tue 10:00) to accommodate" so you can decide.

### "They said one of my times works"

No model stages this invite. The ten-minute intake and the hourly
check find the reply that takes a time from the user's last offer in
the thread, and `sched_acceptance.py` builds the event: the thread's
subject, the accepted slot on the user's clock, the length from the
reply, the offer ("a 30-minute Zoom") or the preference
`preferred_duration_minutes`, the one who accepted as attendee (the
rest of the thread optional), and the user's video link unless the
thread says phone or in person. `validate-draft` checks the slot; a conflict is
named in the thread, and the Create card waits for the click. A turn
then drafts the confirming reply. Code hands the thread to the model
instead, with its reason in the first line, when the offer went to
several people or to someone else, the reply hedges ("probably",
"instead"), the reply took a window longer than the meeting, no length
is known, or the time has passed.

```bash
python3 preprocessor/sched_thread_state.py analyze --thread-file thread.json   # what the thread says
python3 preprocessor/sched_backtest.py --days 14                               # what code would have staged
```

## Scheduler trace (the Slack guidepost)

Every Slack reply that involved the scheduling engine carries a
visible footer showing exactly which tool fired, with key args and
result count. Format:

```
Tuesday May 26: 9:00, 10:30, 10:45, 11:00, 11:15, 11:30 AM all free.
Want me to draft a reply?

─────
:calendar: propose-meeting (duration=30, days-ahead=7, attendees=2) → 6 options /1.2s
```

This is appended automatically by `ea-chat-bot.py:_format_scheduler_trace`
whenever `cr.tool_calls` contains a call to `scheduling_engine.py`,
`google_calendar_client.py get-events`/`create-event`/`update-event`/
`delete-event`, or `graph_actions.py get-event`/`create-event`/
`update-event`/`delete-event`. Replies that didn't touch any of those
get no footer (so the `:calendar:` marker stays meaningful).

If the trace would push the last reply chunk past 3000 chars (Slack's
practical message-size limit), it's posted as a separate follow-up
message instead.

### Per-exchange dump files

Each scheduling exchange also writes a forensic JSON dump to
`~/.ea/logs/scheduler/<YYYY-MM-DD>/<msg_ts>.json`, but ONLY when ≥1
scheduler tool actually fired (keeps the directory small). The dump
captures the user message, full tool-call args (un-truncated unlike
`slack-bot.log`), elapsed times, result snippets, the Claude text
output, and the trace posted to Slack — everything needed to
reproduce a "this reply was wrong" without re-running the pipeline.

To open the most-recent dump on the host:

```
ls -t ~/.ea/logs/scheduler/$(date +%F)/*.json | head -1 | xargs jq .
```
