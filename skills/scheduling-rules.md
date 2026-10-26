# Scheduling Rules (Single Source of Truth)
<!-- scopes: scheduling -->

All scheduling logic lives here. Other files reference this file
rather than duplicating the rules.

## The calendar is the only source of day/time rules

> **STOP before you reason about availability.** You have NO hardcoded
> scheduling defaults. There is no "9-5", no "Fridays off", no "lunch at noon"
> that you know from memory or from this file. Every working-hours / no-meeting /
> preference rule lives ONLY as a tokened event on the EA-Managed calendar, and
> the solver reads them live on every call. **The `propose-meeting` output IS the
> ground truth. Trust it verbatim.**

Concretely, when you run `propose-meeting`:

- `calendar_preferences.tokens` lists exactly which preference events the solver
  read for this window, grouped by token (`wh`/`nm`/`p0`/`p1`/`p2`/`wr`). **A
  token with an empty list `[]` means that rule does not exist right now.** If
  `nm` is `[]`, there are NO no-meeting blocks: Friday is a normal working day,
  there is no lunch block, nothing is protected. Do not say otherwise.
- `options` are the real, ranked, calendar-checked times. Offer THESE.
- `excluded` says which times were dropped/demoted and by which token.
- Each option's `applied_preferences` says which soft tokens penalized it.

**NEVER apply a day/time preference that is not in `calendar_preferences.tokens`,
even if you remember it being true.** The user deletes and drags these constantly
(that is the whole point of putting them on the calendar). "The user usually keeps
Fridays free" is exactly the stale reasoning to avoid: if there is no `nm` event
on that Friday, it is available, full stop. If you catch yourself setting a time
aside for a reason the solver did not return, you are hallucinating a rule.

Other defaults (these are NOT calendar-dependent):

- Default meeting duration: 30 minutes. NEVER create 1-hour meetings unless the
  user explicitly requests it ("schedule an hour", "60 min meeting"). Applies to
  students, colleagues, and external contacts.
- Always include the user's time zone (for example MT) in emails to external contacts.
- The user's office location is in `user-profile.json` ("office" field). Use that
  for meeting locations. Do NOT guess building names.
- NEVER double-book unless the user explicitly says to.

### Video links (online meetings)

The user's institution names its video platform in `user-profile.json`
(`institution.video`); never create a Teams meeting unless that is the
platform. A calendar invite to an **external attendee** (anyone outside the
institution's domains, `institution.domains`) is an online meeting by
default: the user's video link (from `user-profile.json`) must be attached. This
is now automatic. The bot infers `online=true` for any invite with an external
attendee and no physical room, so `create-event` runs with `--online` and the
video link lands in the body and location. You do not need to remember it. To
override: set `"online": false` in the pending-event JSON for a genuinely
in-person meeting, or set a physical `location` (a room/building). For an
internal online meeting with no external attendee, set `"online": true`
explicitly.

### Calendar invites vs. confirmation emails

When the user asks to "schedule" or "set up a meeting", create a
**calendar invite** — do NOT also send a confirmation email. The
calendar invite IS the notification. Sending both is redundant and
clutters the recipient's inbox. Only send a separate email if:
- The user explicitly asks to ("also email them", "send a note")
- There is additional context that doesn't fit in a calendar invite body
- The scheduling is still tentative (proposing times, not confirming)

### EA-Managed holds (auto-create, no button)

There is a dedicated **EA-Managed Calendar** (a secondary Google calendar,
`ea_managed_calendar` in scheduling-preferences.json) that you MAY write to
directly, WITHOUT a button, for **attendee-less personal holds**:
tentative blocks, focus time, "hold this slot while a poll resolves", travel or
prep buffers. Create one with:

```bash
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py create-event \
  --email <ea_managed_calendar.email from scheduling-preferences.json> \
  --calendar-id <ea_managed_calendar.calendar_id from scheduling-preferences.json> \
  --summary "HOLD: <what>" --start "2026-07-21T14:00:00" --end "2026-07-21T14:30:00"
```

This is the ONLY un-gated calendar write you have. Guardrails (enforced in code,
but follow them anyway):
- NEVER pass attendees to an EA-Managed hold — that would make it a real invite
  and is refused (falls back to the button flow).
- NEVER create on the primary/personal calendar directly. Real meetings, and any
  event with attendees, still go through `pending-event.json` -> :lock: button.
- EA-Managed holds block your availability automatically (the solver treats them
  as busy), so you won't double-book them.

You may also MOVE and DELETE on this calendar without asking. It is your own
rule store: nothing on it has attendees, so nothing you do there mails anyone.

```bash
# start here — you need an event_id to name anything
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py list-ea-managed [--show-instances]
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py update-event --event-id <id> \
    [--start <ISO>] [--end <ISO>] [--summary "p1 ..."] [--recurrence <RRULE>]
python3 /home/ea-bot/ea-system/preprocessor/google_calendar_client.py delete-event --event-id <id>
```

Both verbs default to the EA-Managed calendar and refuse every other one, so
they are safe to reach for, with or without `--calendar-id`.

A hold that has to be visible to colleagues goes on Outlook instead: stage
`pending-event.json` with `"hold": true` and no attendees (the bot creates it
at once, with no button, tentative and tagged "EA Hold"), and release it later with
`python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py clear-hold --event-id <id>`,
no button. Outlook event ids come from `graph_actions.py calendar-view`. A real meeting still goes through
`pending-event-update.json` / `pending-event-delete.json` and its button,
because moving or cancelling it notifies the attendees.

Holds marked `[auto]` belong to the reconciler. It rewrites their times within
30 minutes, so editing one by hand does not stick; change the underlying
meeting instead. Editing the token WORD on an `[auto]` mirror IS preserved,
which is how the user overrides a mirror's protection level.

Be a real assistant with this: when you propose times for a meeting, offer to
HOLD the top option(s) on the EA-Managed calendar so they aren't taken while the
other side decides, and tell the user what you held.

### Preference tokens (how the calendar defines the rules)

The user's scheduling rules ARE events on the EA-Managed calendar. The leading word
of an event title is a token; the rest is a human label the solver echoes back:

| token | meaning | effect on the solver |
|-------|---------|----------------------|
| `wh`  | working hours | meetings proposed ONLY inside these windows |
| `nm`  | no meetings (protected) | hard block (only where an `nm` event exists) |
| `p0`  | hard constraint | hard block |
| `p1`  | soft constraint | strong penalty (ranked well down, overridable) |
| `p2`  | preference | mild penalty (ranked slightly down) |
| `wr`  | working remotely | informational only; solver notes it on the slot |

Events are **color-coded by polarity** so the calendar reads at a glance:
GREEN = available (`wh`), RED = hard no (`nm`/`p0`), AMBER = soft (`p1`/`p2`),
GRAPHITE = informational (`wr`). `create-event` auto-applies the color from the
leading token; after manual Google-UI edits (which don't auto-color), run
`google_calendar_client.py recolor-tokens --email <acct> --calendar-id <cal>` to
normalize. Overlap is expected and fine: an amber `p2` sitting inside a green
`wh` means "available but deprioritized," not a conflict.

You do not manage these; the user edits the calendar. To change their hours for one
week they drag that week's `wh` event; to allow a Friday they delete that Friday's
`nm`. To add a new standing rule, create a tokened recurring event with
`create-event --recurrence "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"`. There is
NO config fallback: if the calendar has no `wh` window for the horizon, the
solver returns a clean error rather than guessing.

**When the user states a scheduling preference or priority in chat** ("prefer
mornings", "block Thursday afternoons", "never book over X"), the correct
persistence is a tokened event on the EA-Managed calendar, not a config edit
and not prose in a skill file. Time-shaped rules live on the calendar.

### `[auto]` mirrors and `[note]` (do not manage these either)

Two more marker classes appear on calendars and both are normal:

- **`[auto]` mirrors** on EA-Managed are written by the calendar protection
  reconciler (a cron pass), one for every timed event on the user's real
  calendars, the family calendar included: one recurring mirror per
  series, one single mirror per one-off meeting (a series with a monthly
  or other pattern gets one per occurrence). EA-Managed is the one view of
  the user's time, and the mirror's first word is the event's priority. The
  SOLVER USES IT: where an event has a mirror, the mirror decides, not
  the raw event.
  - `p0 [auto]`: blocks, never offered away (lectures, group and
    department meetings, a 1:1 with someone outside the institution, a visit).
  - `p1 [auto]`: blocks, but the relaxation pass may offer it as a
    "requires moving X" option (a 1:1 inside the institution); an overbook candidate
    that needs the user's approval (`/home/ea-bot/ea-system/skills/fill-poll.md`).
  - `p2 [auto]`: does not block; a mild penalty (an invite the user has not
    accepted). An option on top of one carries the note "Overlaps X (...,
    p2 on EA-Managed)"; say so when you propose it.
  - `[note] [auto]`: free time, shown for visibility (a team weekly,
    family events that are not the user's). Same "Overlaps" note.
  Defaults come from the event's shape, and a family event mirrors as a
  `[note]`. A level written on the source event itself ("p0 <first name>
  watching the kids" on the family calendar, or `[note]` in an Outlook
  title) wins, and changing it relevels the mirror. A one-off already
  covered by a hand-placed token of the same or a higher level gets no
  mirror. The user edits the level word on a mirror to change the policy;
  the reconciler preserves the edit, managing times only. If the user deletes a
  mirror, the event comes back as a `[note]` mirror; deleting that note
  too takes it off EA-Managed for good. NEVER create, edit, retime, or
  delete an `[auto]` event yourself; never propose a time inside a `p0`.
  A meeting made since the last reconciler run (every 30 minutes) has no
  mirror yet and blocks by the attendee heuristic.
- **`[note]`** anywhere in a title, on ANY calendar, marks an event as
  informational (the house cleaner, a delivery window). It never blocks a
  slot. If the user says an event is "just a note" or should not claim their
  time, suggest tagging its title with `[note]` rather than deleting it.

### Conversational proposals

Propose times the way a human EA would, not as a bare list. From the solver's
`options`, say e.g. "How do Tuesday 2pm, Wednesday 10am, or Thursday 3:30pm
sound?" When the user has travel or other commitments near the candidate dates,
call it out and offer a hold: "You're away Jun 28-Jul 4, want me to
block prep time the day before?"

Every proposed time comes from the solver's `options` — never a time you reasoned
to yourself. If asked why a time was or wasn't offered, answer from
`calendar_preferences.tokens`, `excluded`, and `applied_preferences` (the live
calendar read), quoting the real event labels, not from memory. If the calendar
shows no block on a day you'd expect to be busy (an empty `nm`), trust the
calendar and say so plainly rather than hedging with a remembered habit.

### Preference overrides for important meetings

Preference tokens differ in strength: `nm`/`p0` are HARD blocks; `p1`/`p2` are
SOFT (the slot is still offered, just ranked lower). The solver tells you which
applied, using the events' real labels: every option carries
`applied_preferences` (the soft tokens that penalized it) and the output has a
top-level `excluded` list (times it dropped or demoted, each with a `reason`
naming the token event). SURFACE these — with the actual labels the solver
returned, never invented ones, so the user can override, e.g. "I ranked the two
3:30 slots lower (your 'p2 Prefer mornings' band) — say the word and I'll treat
them equally."

When the solver put a time in `excluded` under a HARD token (`nm`/`p0`) and the
meeting seems important, ASK before overriding, quoting the token's real label:

> ":warning: 2:30 falls in your '<exact nm/p0 label from the solver>' block.
> Since this is with [Dean / program officer / external contact], want me to
> include it anyway?"

Only offer an override for a block the solver ACTUALLY returned. If `nm` is empty
in `calendar_preferences.tokens`, there is nothing to override — do not
manufacture a lunch or Friday caveat.

**When to ask:** External contacts, deans, vice chancellors, sponsors, program
officers, VIPs, anyone who is hard to reschedule.

**When NOT to ask:** Internal 1:1s, student meetings, routine committee
scheduling. Just offer the solver's `options`.

When the user resolves one of these, LOG it (`log-decision`, below). The engine
does not hand you history on a proposal call: `review-patterns` is the command
that reads the log back. If a pattern emerges there, suggest it proactively
(and, for a standing change, suggest they add/edit the corresponding token event
on the calendar).

## Calendar source failures

A dead calendar account is indistinguishable from a free one, so fetch
failures are surfaced everywhere availability is:
- The briefing JSON carries a `calendar_fetch_errors` list.
- `propose-meeting` and `validate-draft` each return their own
  `calendar_fetch_errors` and `warnings` keys, and the warning is also
  appended to every affected option's `notes`. `validate-draft` adds a
  `calendar_fetch_error` issue (a warning: `valid` stays true).

When any of these is non-empty: warn the user
(`:warning: Calendar source <account> failed to load`), do NOT present
proposed times as authoritative, and do NOT fill polls until the source
is back.

## Canonical tool selection

`scheduling_engine.py` is the ONLY source of truth for availability.
It exposes exactly FIVE subcommands (plus `reschedule-brief`, covered
in `/home/ea-bot/ea-system/docs/ea/scheduling.md`). Pick by the question:

| Question | Subcommand | Returns |
|---|---|---|
| "Is THIS specific time free on the USER's calendars?" | `validate-draft` | `valid`, `issues`, `all_day_events`, `ignored_events`, `checked_at` |
| "What times work for a NEW meeting?" | `propose-meeting` | `status`, `options`, `conditional_options`, `excluded`, `infeasible_days`, `all_day_events`, `attendees_checked`, `ignored_events`, `calendar_preferences`, `solver_time_ms` |
| "Are THESE people free in THIS window?" | `check-attendees` | `attendees` (per person: `status` free/busy/tentative/partial/unknown, `not_free` runs), `all_attendees_free`, `organizer_events` (the user's own events in the window, with ids), `organizer_free`, `checked_at` |
| "Record what the user chose" | `log-decision` | `{status: logged, entry}` (after the fact, for pattern learning) |
| "What patterns did the logged decisions show?" | `review-patterns` | `{patterns, suggestions, total_decisions}` |

`check-attendees` is for a FIXED window: "can Jordan join my Tuesday
1:1", "does 2:00 work for both of them". It reads the attendees'
free/busy (Graph getSchedule, same tenant) and lists the user's own
events in the window with their ids. It does not rank or apply
preferences; open "when could we meet" questions stay with
`propose-meeting`.

**Ignoring one of the user's own events.** `propose-meeting`,
`validate-draft` and `check-attendees` take `--ignore-event <id>`
(repeatable). The named event is treated as free for that call, so a
slot the user already holds can be scored ("add Jordan to the 11:15
1:1", "could the seminar move into my hold"). Ids come from
`check-attendees` `organizer_events` or `graph_actions.py calendar-view`
(Outlook `id`) and `google_calendar_client.py get-events` (Google
`event_id`). The output's `ignored_events` lists what was lifted;
`ignore_not_found` names any id that matched nothing, which means the
event still blocks.

Note what is NOT in either proposal output: decision history. See
"Decision history" below.

### Canonical examples

**Specific time check** — someone proposed 9:30 AM, is it free?
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py validate-draft \
  --times '["2026-05-26T09:30:00-06:00"]' --duration 60
```

**Open propose** — find times for a new meeting:
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting \
  --duration 30 --days-ahead 7 --num-options 5 \
  --attendees '[{"email":"person@example.edu","name":"Name","type":"internal"}]'
```

**People check**: can Jordan join the Tuesday 11:15 1:1 with Sam?
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py check-attendees \
  --attendees jordan@example.edu,sam@example.edu \
  --start 2026-10-06T11:15:00 --end 2026-10-06T12:00:00
```
`organizer_events` will list the 1:1 itself; that is expected, not a
conflict. Report each person's `status` and stop.

**Decision logging** — after sending, train future runs:
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py log-decision \
  --event "1:1 with Jordan" --action moved \
  --for "Dr. Smith meeting" --contact "smith@example.com" --contact-type external
```

TRUST THE OUTPUT of whichever subcommand you call, for what it reads.
Do NOT re-check by querying calendars directly. `validate-draft` reads
the USER's calendars only: it says nothing about anyone else. Before
you tell the user that a time works for other people, run
`check-attendees` for them on that time, with the meeting's real
length, and report what it returned; never write that you checked
their calendars after `validate-draft` alone. An attendee
`propose-meeting` lists as "NOT CHECKED" (or `check-attendees` as
`unknown`) has an unreadable calendar: say so, do not call them free.
NEVER infer "X is blocked" from "X is not in `propose-meeting`'s
options" — `propose-meeting` returns RANKED options, not an exhaustive
enumeration. If a specific time matters, call `validate-draft` for it.
If the question is whether named people can make a specific window,
call `check-attendees` for it.

Before you write that you could not check something, name the verb
you ran and what it returned. A window the solver blocks because of
the user's own event is not "unchecked": run `check-attendees` on it,
or re-run with `--ignore-event`. Write "I cannot check this" only
when no verb above covers the question, and say which ones you tried.

## Forbidden subcommands (DO NOT call)

These subcommands DO NOT EXIST. Trying to call them will fail with
"invalid choice" — but more importantly, the right answer is *always*
one of the five canonical commands above:

| Forbidden | Use instead |
|---|---|
| `new-meeting` | `propose-meeting` (single call replaces the whole multi-step workflow) |
| `add-attendee` | `propose-meeting --attendees '[...]'` (pass them all in one go) |
| `add-constraint` | `propose-meeting --constraints '[...]'` (same) |
| `solve-meeting` | `propose-meeting` (it IS the solver) |
| `find-slots` | `propose-meeting` |
| `check-slots` | `validate-draft` (single-time check) |
| `get-availability` | `propose-meeting` for an open question (don't fetch raw availability and reason over it; let the solver do that); `check-attendees` when the window is fixed |

## Absolute paths only

ALWAYS use the absolute path to scheduling_engine.py:
`/home/ea-bot/ea-system/preprocessor/scheduling_engine.py`. NEVER use
`preprocessor/scheduling_engine.py` (relative) — the bot's allowlist
rejects it, costing a turn for nothing.

Same rule for every preprocessor script: `graph_actions.py`,
`gmail_client.py`, `google_calendar_client.py`, `ledger.py`, etc.

## Date hygiene

When the user names a weekday + day-of-month (e.g. "Tuesday May 27"),
DO NOT assume the combination is correct. Verify before you act:

- `date -d "2026-05-27"` — prints "Wed May 27 2026" → not Tuesday
- OR check the briefing's `generated_at` field (today's date) and
  count forward

If the weekday and day-of-month disagree, ASK the user which they
meant. Don't silently pick one. Today's date IS in the briefing —
don't guess from training data.

## Scheduling modes

**ALWAYS read the full email body** (`get-message`) for any scheduling
request before drafting a response. The body determines which mode.

**First: is someone else scheduling it?** When the thread hands the
scheduling to another person (the requester's assistant, scheduling
liaison or coordinator: "copying our scheduling liaison Sam; Sam,
can you find a time"), or a participant says they will send times or a
poll, do not run the scheduler and do not propose times. Say who is
coordinating and wait. When their times or poll arrive, that email is a
Mode A request. Propose times of your own only if the user asks.

### Mode A - Poll response (sender provides specific options)

The sender lists dates/times and asks "which work for you?" Common for
group meetings, advisory boards, committee scheduling.

1. Parse ALL offered date/time options from the email body.
2. Use `propose-meeting` — the sender's offered times become an
   external attendee's availability:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting \
     --duration <N> --num-options 999 \
     --attendees '[{"email":"poll@placeholder","name":"<sender>","type":"external",
       "availability":[{"day":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM"},...]
     }]'
   ```
3. The solver returns ranked feasible slots (options) and conditional
   slots (movable 1:1s). All-day event handling, preference violations,
   and conflict classification are done by the solver automatically.
4. Post to chat with the breakdown:
   - Solver `options` → :white_check_mark: free slots
   - Solver `conditional_options` → :warning: movable conflicts
   - Slots not in either → :x: hard conflicts
5. Wait for user to pick slots. THEN draft the reply with those slots.
6. Reply to sender ONLY (`--sender-only`). Do NOT reply-all on
   scheduling polls — don't broadcast availability to all recipients.
7. Create draft via `create-reply --sender-only`. Save the draft_id.

### Mode B - Propose times (someone asks "when are you free?")

No specific options given. You propose times using `propose-meeting`.

**Single command — builds attendee JSON, calls solver, gets results:**
```bash
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting \
  --duration 30 --days-ahead 7 --num-options 5 \
  --attendees '[
    {"email":"jordan@example.edu","name":"Jordan","type":"internal"},
    {"email":"jane@partner.example.org","name":"Jane","type":"external",
     "availability":[{"day":"2026-04-22","start":"12:00","end":"17:00"}]}
  ]' \
  --constraints '[
    {"type":"preferred_slot","day":"2026-04-22","start":"14:00","end":"15:00","weight":2.0}
  ]'
```

Attendee types:
- `internal`: an address on the institution's domains, calendar checked via Graph API automatically
- `external` — provide `availability` windows (parsed from their email)
- `"timezone":"Europe/London"` on an attendee elsewhere: the solver keeps
  the options inside their working hours (9:00 to 17:00 local, or
  `"hours":["10:00","16:00"]`), so a UK colleague is never offered 10 PM
- An attendee can be BOTH internal and have stated availability constraints
- An open proposal returns separate windows; offered times (an attendee
  with `availability`) and `--num-options 999` list every feasible
  quarter-hour start inside the window

You receive ranked `options` (clean slots) and `conditional_options`
(slots that require moving a 1:1 or extending hours). Format both
into the reply:
- Show clean options as the primary suggestions
- Show conditional options as "also possible if..." alternatives
- Use the `notes` field for context ("adjacent to your 1:1 with X")
- Show time ranges, not just start times
- Spread across multiple days for flexibility

After solving:
1. For internal addresses (the institution's domains): draft directly.
2. For external/new contacts: ask-first.
3. Create draft via `create-reply`. Save the draft_id.

### Mode C - Booking link (cal.com)

The sender points to their cal.com page ("book a time on my cal.com",
"if your assistant can use https://cal.com/<user>/<event>"), or the
user hands you one. The link is a read-only source of that person's
free time. NEVER book through it: do not open the booking page, fill
its form or send anything to cal.com. The user's own scheduler makes
the meeting, as in Mode B.

1. Run `booking_link.py propose` once, with every attendee. Give each
   person who sent a link a `"booking_link"`. Take their address from
   the email headers, not from the link:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/booking_link.py propose \
     --days-ahead 14 --num-options 5 \
     --attendees '[
       {"email":"jane@example.edu","name":"Jane","booking_link":"https://cal.com/jdoe/60min"},
       {"email":"sam@example.edu","name":"Sam","booking_link":"https://cal.com/sroe/vip?duration=30"},
       {"email":"alex@example.edu","name":"Alex"}
     ]'
   ```
   Each link's open slots become that person's stated availability. A
   person on the institution's domains is checked by free/busy as well. The meeting
   takes the first link's length unless you pass `--duration`. A Safe
   Links URL works as it is. The output is `propose-meeting`'s, plus a
   `booking_links` block: what each link offered, and for which days.
2. Propose the options as in Mode B, and wait for the user to choose.
3. Make the meeting: stage `pending-event.json` with every attendee's
   address, `"online": true` and `"thread_ts"`. The Create button sends
   the invite from the user's calendar. The invite is the answer to the
   sender; draft a reply email only when the user asks for one.
4. Nothing is held on cal.com. When the user picks more than a few
   hours after the proposal, run step 1 again before you stage.

A link the user got privately (a "vip" or hidden event, or one sent to
the user alone) stays private. Never put it in an email or invite to
anyone else.

`booking_link.py read --url <link>` shows one link's open windows. A
profile link (`cal.com/<user>`) returns that person's event list: use
the event the sender meant. Other booking pages (Calendly, cal.com team
links) cannot be read. Say so and ask for times.

### Both modes

- If user requests changes to the draft, use `update-draft` with the
  saved draft_id. NEVER call `create-reply` again for the same thread.
- Post the draft text to chat for approval before creating it.
- "Reply *more* for more times, edit to change, or I'll create the draft for your approval."
- After creating the draft, the bot posts a confirmation block. The user approves via their configured method.

### After every scheduling interaction

**MANDATORY**: After scheduling is resolved (user approves/corrects and
sends), log the decision. This is how the system learns preferences.

Log what happened:
```
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py log-decision \
  --event "1:1 with Jordan" --action "moved" \
  --for "Dr. Smith meeting" \
  --contact "smith@example.com" --contact-type "external" \
  --comment "user said students are easy to reschedule"
```

If a preference was overridden (user approved lunch/Friday/after-hours):
```
python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py log-decision \
  --event "lunch block" --action "overrode_lunch" \
  --for "Meeting with Dean Lopez" --override "lunch" \
  --contact "lopez@example.edu" --contact-type "dean" \
  --comment "user said 'go ahead, this is important'"
```

**Fields to capture:**
- `--event`: What was affected (event name, or "lunch block", "friday")
- `--action`: moved, kept, cancelled, overrode_lunch, overrode_friday, overrode_hours
- `--for`: The meeting that caused the decision
- `--override`: lunch, friday, or after_hours (if a preference was overridden)
- `--contact-type`: external, dean, student, vip, sponsor, colleague, etc.
- `--contact`: Email or name of the person the meeting is with
- `--comment`: User's own words if they explained their reasoning

## All-day event judgment

`propose-meeting` and `validate-draft` BOTH return a top-level
`all_day_events` list. The engine classifies each one and tells you what it
already did; the remaining judgment is yours.

Each record:

```json
{
  "subject": "IROS travel",
  "is_all_day": true,
  "start_date": "2026-10-05",       // inclusive first day
  "end_date":   "2026-10-09",       // INCLUSIVE last day
  "showAs":     "oof",
  "calendar":   "Outlook",
  "classification": "hard_block",   // the engine's read
  "effect":     "blocked",          // what it actually applied
  "note":       ""                  // e.g. "calendar does not block availability"
}
```

`classification` -> `effect` mapping, applied in code:

| classification | effect | what the solver did | matched on |
|---|---|---|---|
| `hard_block` | `blocked` | every slot on those days removed | `showAs: "oof"`, or a title containing conference / travel / vacation / ooo / flight / out of office / away / retreat |
| `soft_penalty` | `penalized` | slots still offered, ranked down (`all_day_hold_tentative`) | hold / tentative / placeholder / block |
| `ignore` | `none` | nothing. Not in `issues` at all, so the send-time preflight does not warn on a birthday. | deadline / reminder / birthday / exam / exam week / anniversary / task |
| `unknown` | `penalized` | ranked down under `all_day_unknown`, and surfaced for YOUR call | anything else |

`showAs` only ESCALATES. Outlook marks all-day events `free` by default, so
a Free conference week still hard-blocks on its title; an explicit
`oof` blocks whatever the title says. A title that is not in any of those
word sets ("IROS 2026", "Dept review") lands in `unknown` — that is the
one the engine is telling you it could not read.

A calendar configured `blocks_availability: false` (a spouse's calendar)
forces `effect: "none"` with a `note` saying so, whatever the
classification. The record is still reported so you can overrule.

On `validate-draft`, a `hard_block` covering a proposed date is emitted as a
`conflict` issue, so `valid` comes back **false**. A `soft_penalty` or
`unknown` becomes an informational `all_day_event` issue and does NOT fail
validation.

Two record types are deliberately absent (they return `None` and never
appear): `[note]` events, which are informational by definition, and
EA-Managed token events (`wh`/`nm`/`p0`/`p1`/`p2`/`wr`), whose meaning is
already applied as a scheduling constraint and must not be double-counted.

**Your job** is the `unknown` ones, and any `hard_block` you think the
engine read wrong:

- Conference / travel / workshop -> confirm the block, and offer to hold
  prep or recovery time around it.
- Reminders, tasks, household items -> tell the user you are ignoring it, or
  suggest tagging the title `[note]` so it stops costing them time.
- Genuinely uncertain -> ask in chat, quoting the subject and the dates.

When the user answers, `log-decision` it so `review-patterns` can suggest a
standing rule later.

## Flexible conflicts

When the user marks a meeting as high priority or says "I can move X"
or "I can skip Y to make this work":
- Treat the named meeting types as *soft conflicts*.
- Show as: ":warning: [time] - conflicts with [meeting], movable"
- Pass `movable_1on1` work to the solver rather than reasoning over it:
  `propose-meeting` already runs a second, relaxed pass when the primary
  pass returns fewer options than asked, and returns those separately as
  `conditional_options`, each carrying the relaxation that bought it.
  Offer conditional options AS conditional ("Thursday 2pm works if we move
  your 1:1 with Jordan").

NEVER silently ignore a conflict or silently move a meeting.

## Decision history (`log-decision` / `review-patterns`)

The engine does NOT return decision history on a proposal call. There is no
`past_decisions` key in `propose-meeting` or `validate-draft` output. The
history is a write-then-read pair:

- `log-decision` appends one record to
  `~/.ea/state/scheduling-decisions.json` (last 50 kept).
- `review-patterns` reads that file back and is the ONLY way to see
  patterns. It takes no arguments.

So: if you want to know how the user handled something like this before, run
`review-patterns`. Do not expect it to arrive with the options.

`review-patterns` returns `{patterns, suggestions, total_decisions}`:

- `patterns`: one entry per event type with 2+ records —
  `{event_type, total, dominant_action, rate, breakdown}`.
- `suggestions`, generated on fixed thresholds:
  - `add_vip` — a contact with 2+ logged overrides
  - `update_preference` — the same override used 3+ times
  - `soft_classify` — an event type moved in >=80% of 3+ records

Present them in chat:
> ":chart_with_upwards_trend: Based on your logged scheduling decisions:
> - You've overridden lunch 4 times for external contacts. Want me to
>   auto-suggest lunch slots for external meetings going forward?
> - Student 1:1s were moved 5/5 times. Should I treat these as soft
>   conflicts by default?
> - Dr. Smith has been a priority 3 times. Add to VIP list?"

A TIME-shaped rule the user approves belongs on the EA-Managed calendar as a
token event, not in a config file. `scheduling-preferences.json` is for
non-time settings (VIP list, per-contact overrides, solver weights).

## Calendar event creation

- For work meetings with internal contacts: use Outlook
  `create-event` via `pending-event.json` -> the :lock: Create button
  (it sends proper Outlook invites).
- For personal/family events: also `pending-event.json`, with
  `"calendar": "<Google account>"` (an address from `calendar_accounts` in
  `user-profile.json`) and
  no attendees to put it on that Google calendar. The same key on
  `pending-event-update.json` / `pending-event-delete.json`, with the
  Google `event_id` from `get-events`, moves or deletes it there. Do NOT
  reach for
  the Google Calendar MCP write verbs; they are denied in
  `/home/ea-bot/ea-system/scripts/disallowed-tools.txt` on every lane. The one un-gated calendar
  write you have is the EA-Managed hold described above.
- Before creating ANY event, CHECK for conflicts at that time with
  `validate-draft` (the user's calendars) and, when it has invitees,
  `check-attendees` (theirs). If a conflict exists, tell the user in
  chat before creating. The Send card and the Create card also show
  any invitee who is not free then.
- Read `scheduling-preferences.json` for full preference details.
