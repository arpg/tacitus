# Web chat: the state of a conversation (contract, v0)

Every thread in the web chat gets one **state**, shown as a sticker on
its card, and a **Needs you** view gathers the threads that wait on the
owner across every channel: mail that needs a reply, a question the bot
asked, a card with live buttons, a scheduling thread waiting for a pick.
Rules set the state when the evidence is decisive; a model sets it when
it is not. The owner can override with one tap.

The bot side is `botcore/botcore/chat/thread_state.py` (the rules, the
model step's prompt and parser, the engine) and the store and routes in
`web_frontend.py`; the EA's ledger read and model route are
`preprocessor/web_thread_state.py`. "Changes from v0" at the end lists
where this page departs from the contract as first agreed.

## States

| state | meaning | sticker (plain words) |
|---|---|---|
| `needs_you` | the owner's next action is required | "Needs you" |
| `working` | the bot is on it (its hourglass is up, or the owner's last message has no reply yet) | "Working" |
| `waiting` | waiting on someone else or on a date (a reply from a third party, a snooze, a deadline ahead) | "Waiting" |
| `done` | closed: nothing left to do here | "Done" |
| `none` | no action implied (a briefing, a digest, a notice, small talk) | no sticker |

`by` says who set it: `rule`, `model` or `owner`. An `owner` state is
sticky: rules and the model do not change it until a new message lands
in the thread after the owner set it. `reason` is one short plain
sentence for the owner ("The card still has its Send button", "You asked
for the budget draft and the bot has not answered", "The model read the
thread: the colleague has not replied yet").

## Rules (decisive, in this order; the first that fires wins)

1. Owner override newer than the last message: keep it.
2. A card in the thread whose buttons are live: `needs_you`. A card is
   live while its row is still a `card` (the bot has recorded no outcome:
   a pressed card becomes a `status` row with the bot's status line), it
   has an action or a select, and it is younger than the card registry
   keeps cards (7 days; an older card's buttons no longer act).
2a. The relay's notice of a new device (its text names the pairing code):
   `needs_you` ("A phone waits for your approval.") while a device with
   that code waits for approval, `done` once it is approved or removed.
   With no relay running the rule waits, and rule 7 applies.
3. The owner wrote last, and the bot has posted nothing since but
   progress lines (`:hourglass...`) or a failed-turn notice (`:x:`,
   `:warning:`):
   - a failed turn (the `x` or `warning` reaction on the owner's message,
     or a failed-turn notice): `needs_you`, "Tacitus did not answer this.
     Ask again.";
   - within 20 minutes of the owner's message (or of the bot's last
     progress line): `working`;
   - past 20 minutes: `needs_you`, "Tacitus did not answer this. Ask
     again." (a stuck turn).
   The bot's check on the owner's message (a finished turn with no words)
   and a message copied from Zulip's past (its reactions are not copied)
   are not decisive here.
3c. A card whose status line shows the bot acting on the press
   (`:hourglass...`, "Sending...") within the last 20 minutes: `working`.
4. A card acted on (its status line as the bot records it: Sent, Created,
   Handled, Cancelled; not a failure, `:x:` or `:warning:`) and no later
   message that asks anything (a later owner message, or a bot message
   with a question mark outside links and code): `done`.
5. Dropped. The bot's `white_check_mark` sits on nearly every owner
   message, including threads that ended open: it marks a finished turn,
   not a closed conversation. A thread that ends on the bot's prose goes
   to the model; silence for some hours is no rule either.
6. A ledger row linked to the thread (by a conversation id or a
   `ledger_id` in a card, or a row id in the text): an `open` or
   `follow_up` row of type `response_needed`, `scheduling` or
   `draft_pending` -> `needs_you`; else a row whose status is `waiting` or
   `snoozed` -> `waiting`; else, when every linked row is `completed` ->
   `done`. An open row of any other type (`task`, `deadline`, `waiting`,
   `reimbursement`, `dispute`) is no rule: the thread goes to the model
   with the row's type, status, deadline and last action.
7. The thread's first message is from the bot and is a briefing, digest, alert, new-device notice or other automated post (the notifier key is known when the post was made; otherwise the known title patterns), and the owner never wrote in it: `none`.
8. Otherwise: ask the model.

**Shadow mode: mirrored cards read their outcome from Zulip.** A card
the history copy took from Zulip is a plain text row (no card JSON,
`history` 1, in a `stream:<n>` channel, its text opening with the lock
title and holding the card's `----` rule). Rules 2, 3c and 4 read that
text the way the Zulip frontend writes it
(`zulip_frontend.card_text_state`). The action legend after the rule
(`Tap a reaction below:`, `` Reply `<word> ...` ``, `Code **...**`) makes
it a live card: `needs_you`, `The card "Confirm send" still has its
buttons.` A status line in the legend's place makes it a card acted on:
`done`, with that line in plain words ("The card was answered: Sent from
Outlook."), unless a message after the card asks something (rule 4; any
message posted after the card counts, since the time of the edit is not
always known). A busy line (`:time_ticking:`) is `working` within 20
minutes of Zulip's last edit. A failed line (`:cross_mark:`, `:warning:`,
`:no_entry:`) is not settled. A card that Zulip filed in the
`<dm_stream>-done` stream was settled outside chat. While the stored text
still shows the legend or a busy line, the tick asks Zulip for the card
as it is now (`card_now`: a read by message id, else the first message
of the topic by stream and topic name; and the stream it sits in now).
It makes 30 reads per tick at most and keeps each answer for one tick
interval; a card past the cap keeps its stored text until the next tick.
A changed text goes into the row (`mirror_update`, one `update` event; an
unchanged text writes nothing), so the app shows the status line too.
With no Zulip (the web chat alone) the stored text is all there is. The
one-line pointer to a card ("The Send button for this draft is in your
DM") has no rule and is not a card.

## The model

Task `thread_state` in the routing table (docs/model-routing.md), `local`
by default: the local endpoint (`remote_llm.chat`, purpose
`chat-thread-state`, with the answer's JSON schema), as `tagging` uses it.
`auto` means the same, since the task has no switch of its own. With no
local model set up, or set to `hosted`, the rules alone set the state and
the bot logs why once an hour: the hosted step is not built (a hosted
call needs a Claude session). Input: the thread's messages (author, time,
text, card titles and outcomes; never a card's body, fields or quoted
mail), the channel's kind and name, the linked ledger rows' type,
status, deadline and last action, today's date. The prompt is
`thread_state.PROMPT`. Output, JSON only: `{"state": one of the five,
"reason": "<one sentence in plain words>", "confidence": 0..1}`, parsed
strictly (one object, a code fence allowed, nothing else; exactly those
three keys). The reason kept is its first sentence without Markdown or
emoji codes. Below 0.6 confidence, and on an answer that does not
parse, the thread keeps its previous state (`none` when it had none),
`by` is `model` and the reason says the model was unsure or gave no
answer it could use. A call that no model answers writes nothing; the
next tick asks again.

When the model runs: a tick every 5 minutes. The tick first runs the
rules over every thread with a message in the last 14 days (newest
first, at most 500; channels not archived), so a rule that turns with
time (the 20 minutes, a card's age, a ledger row closed, a phone
approved) moves the sticker with no new message. It then asks the model
about the threads the rules leave open whose newest `seq` is past the
`judged_seq` recorded for them, or whose state a rule set and no rule
holds now: 20 calls per tick at most, newest first, and none after a
call that no model answered. The first tick runs at start. A change the
rules cannot settle brings the next tick forward to one minute after the
last one. Rules run at once on every event that changes a thread (a
message, a card outcome, a reaction, in the bot or a command-line
sender: the engine reads the store's event log every second), so a
sticker never waits for the model when a rule is decisive.

## Store

New tables in `webchat.sqlite`, made when the store opens:

```
thread_state(channel TEXT, thread_key TEXT, state TEXT, by TEXT, reason TEXT,
             at REAL, judged_seq INTEGER, PRIMARY KEY (channel, thread_key))
thread_origin(channel TEXT, thread_key TEXT, origin TEXT, at REAL,
              PRIMARY KEY (channel, thread_key))
```

`judged_seq` is the newest message seq the state was judged on (for the
owner's override, the newest when the owner set it). `at` changes when
the state, `by` or the reason changes; a write that changes only
`judged_seq` is silent. A rule's or the model's write never goes back:
it is dropped when the row was judged on a newer message, or when the
owner set it on the same message or a newer one. `thread_origin` holds
the notifier key (`chat_notify`'s channel key, `notifier` for its other
posts) of an automated post that opened the thread; rule 7 reads it. A
moved thread (a renamed Zulip topic) takes both rows with it.

Each override is also a labelled example: one JSON line in
`thread-state-labels.jsonl` beside the store (the owner-private dir,
0600), `{"at", "channel", "thread_key", "state", "previous_state",
"previous_by", "message_count"}`, never any text, so the model step can
be scored later.

## API (under `base_path`, owner-only like the rest)

- `GET /api/channels/<id>/threads` (the existing threads route) rows gain `state`, `state_by`, `state_reason`, `state_at`; a thread with no state row has `"state": "none"`, `state_by` and `state_at` null and `state_reason` "".
- `GET /api/needs` -> `{"threads": [...]}`: every thread whose state is `needs_you` or `waiting`, in channels that are not archived, each row as the threads route gives it plus `channel` and `channel_name`, `needs_you` first, then newest first (by `last_at`). Cap 200. One query; it moves no read mark (the app calls it on every stream connect and after each `thread_state` event). `needs` is a reserved channel id: the store refuses a channel with it.
- `POST /api/threads/<channel>/<thread_key>/state` (both parts URL-encoded) body `{"state": "done" | "needs_you" | "waiting"}` -> the thread's row as the threads route gives it, with `channel_name`; `state_by` becomes `owner`. Only those three are settable (400 for anything else, 404 for an unknown channel or thread; a message id names its thread). Through the relay this is a chat action, allowed for an approved device at "chat" (it changes no mail and sends nothing); a device kept off the card buttons may still take it.
- SSE event `thread_state`: `{"channel", "thread_key", "state", "by", "reason", "at"}` on every change.

A threads row:

```json
{"seq": 41, "channel": "dm", "id": "w41", "thread_key": "t41", "author": "bot",
 "kind": "card", "text": ":lock: **Confirm send**", "card": {"...": "..."}, "file": null,
 "reactions": [], "posted_at": 1793570000.0, "updated_at": 1793570000.0,
 "reply_count": 0, "last_at": 1793570000.0, "state": "needs_you", "state_by": "rule",
 "state_reason": "The card \"Confirm send\" still has its buttons.", "state_at": 1793570001.2}
```

A needs row is the same with `"channel_name": "dm"`.

## The app

- **Sticker** on each thread card: top-right beside the time. `needs_you` in the accent colour with weight, `waiting` in the warn colour, `working` muted with a small pulse (none under reduced motion), `done` muted with a check. `none` shows nothing. The reason is the card's `title` attribute and is read by screen readers.
- **Needs you** at the top of the channel list, above the DM, with a count badge of `needs_you` threads; tapping it shows the `/api/needs` list, each card carrying its channel's name under the title; opening a card goes to that thread in its channel (the hash route).
- **Filter chips** at the top of a channel's thread list: All, Needs you, Waiting, Done. The choice is per browser (localStorage), default All.
- **Override**: in the thread header a small button "Mark done" (or "Needs me" when the state is done); long-press on a card does the same on a phone. It calls the state route; the sticker updates from the SSE event.
- The app keeps every id and class the tests pin; new strings are plain words; the line cap in `test_the_app_stays_small` rises with a reason line.

## Out of scope for v0

Writing back to the ledger (an owner's "Done" does not close a ledger
row; a later version proposes a closure). Zulip gets no stickers; in
shadow mode the state lives in the web store only. The hosted model
step.

## Changes from v0

The contract as first agreed is the first commit of this page. The
bot side changed it here:

- **From the closure analysis** (the owner's past Zulip conversations,
  cards and ledger rows): rule 5 (the bot's check => done) is dropped;
  rule 3 has a second half (no answer past 20 minutes, or a failed turn,
  is `needs_you`: "Tacitus did not answer this. Ask again."); rule 6
  hands an open row of a type outside the three to the model with its
  deadline and last action, and reads `waiting` as a status; rule 2a
  (the new-device notice) is new; overrides are logged as labelled
  examples; the model's reason is cut to one sentence.
- **The threads route** is the existing `/api/channels/<id>/threads`,
  not `/api/threads?channel=<id>`.
- **Rule 2** counts a card older than the card registry's 7 days as not
  live. **Rule 3c** (the bot acting on a press) is added beside rule 3.
  **Rule 4** does not count a failed status line (`:x:`, `:warning:`) as
  acted on, and "asks anything" is a later owner message or a bot
  question mark.
- **Rule 7** reads a notifier key that `chat_notify` records in
  `thread_origin` when it posts.
- **The model**: `auto` runs like `local`; the hosted step is not built
  (rules alone, with a log line); an answer that does not parse keeps
  the previous state like low confidence does; a call no model answers
  writes nothing.
- **The tick** runs the rules over every active thread (cap 500) before
  the model pass, re-asks the model about a rule's state that no longer
  holds, and comes forward to one minute after a change the rules left
  open.
- **`/api/needs`** leaves out archived channels, and `needs` is a
  reserved channel id. **The POST** answers the thread's row (with
  `channel_name`), as the app reads it.
- **The store** writes never go back (a stale model answer or a rule
  never replaces a newer judgment or the owner's override), and a moved
  thread takes its state.
- **Mirrored cards** (shadow mode): a card copied as text from Zulip's
  past is read by rules 2, 3c and 4 from its Zulip text, and re-read from
  Zulip while it can still change (30 reads a tick, each answer kept one
  tick), so the model is not asked about a card that is live or settled
  there. A failed or expired one goes on to the later rules and the
  model, which sees it as a card. Its row takes Zulip's current text.
  The status-line rules also know Zulip's emoji names (`:time_ticking:`
  busy, `:cross_mark:` failed).
