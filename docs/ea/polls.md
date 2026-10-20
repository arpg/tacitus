### Scheduling poll filling (When2Meet, WhenIsGood, Doodle, Timeful, Calendly polls)

When the user asks you to fill a scheduling poll:
1. Find the poll URL from the referenced email
2. Read the poll: `python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py read-poll --url <url>`
3. Use the propose-meeting solver: treat the poll as an external attendee
   whose availability is the poll's time grid. The solver checks the user's
   calendar and returns which slots are free vs conflicted.
4. Post conflict summary to chat, ask about standing meeting overbookings
5. Fill: `python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py fill-poll --url <url> --available-file <path-to-json-array>`
   (the filler puts the user's configured name on the poll and refuses any other `--name`)
   (`--available '<inline JSON>'` also works, but the file form avoids
   hand-transcription errors on long slot lists)
6. Report what was filled and any overbookings

Read `skills/fill-poll.md` for the full workflow. The poll filler enforces
a hardcoded URL allowlist (when2meet.com, whenisgood.net, doodle.com,
timeful.app, framadate.org and calendly.com only).
This is ask-first tier: always show the conflict summary before filling.

A Calendly *meeting poll* (`calendly.com/d/<code>/...`) is a vote and is
filled like the others; a Calendly booking page is read-only
(`booking_link.py`), never booked. The read needs no browser. The fill
stops before the submit and leaves a screenshot unless `--go` is given:
run it without `--go` first, show the user the screenshot path, and run
it again with `--go` when they say so.
