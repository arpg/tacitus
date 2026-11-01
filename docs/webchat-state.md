# Web chat: the state of a conversation (contract, v0)

Every thread in the web chat gets one **state**, shown as a sticker on
its card, and a **Needs you** view gathers the threads that wait on the
owner across every channel: mail that needs a reply, a question the bot
asked, a card with live buttons, a scheduling thread waiting for a pick.
Rules set the state when the evidence is decisive; a model sets it when
it is not. The owner can override with one tap.

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
2. A card in the thread whose buttons are live (no outcome recorded): `needs_you`.
3. The thread's last message is the owner's and the bot has an hourglass on it, or the bot has not answered yet (within 20 minutes): `working`.
4. A card acted on (Sent, Created, Handled, Cancelled) and no later message that asks anything: `done`.
5. The bot's `white_check_mark` on the thread's first message and no later owner message: `done`.
6. A ledger row linked to the thread (by conversation id in the card, or the row id in the text): the row's status maps `open`/`follow_up` -> `needs_you` when `type` is `response_needed`, `scheduling` or `draft_pending`; `waiting`/`snoozed` -> `waiting`; `completed` -> `done`.
7. The thread's first message is from the bot and is a briefing, digest, alert, new-device notice or other automated post (the notifier key is known when the post was made; otherwise the known title patterns), and the owner never wrote in it: `none`.
8. Otherwise: ask the model.

## The model

Task `thread_state` in the routing table (docs/model-routing.md): the
local model by default (the tool gateway on loopback, like `tagging`),
hosted when the owner routes it there. Input: the thread's messages
(author, time, text, card titles and outcomes), the channel's kind and
name, the linked ledger row's type and status if any, today's date.
Output, JSON only: `{"state": one of the five, "reason": "<one sentence
in plain words>", "confidence": 0..1}`. Below 0.6 confidence the thread
keeps its previous state and the reason says the model was unsure. The
model never sees mail bodies beyond what the thread already shows.

When the model runs: a tick every 5 minutes over threads with a message
in the last 14 days whose newest `seq` is past the `judged_seq` recorded
for them, 20 threads per tick at most, newest first; and once at start
for threads that have no state yet (same cap, then the tick catches up).
Rules run at once on every event that changes a thread (a message, a
card outcome, a reaction), so a sticker never waits for the model when a
rule is decisive.

## Store

New table in `webchat.sqlite`:

```
thread_state(channel TEXT, thread_key TEXT, state TEXT, by TEXT, reason TEXT,
             at REAL, judged_seq INTEGER, PRIMARY KEY (channel, thread_key))
```

## API (under `base_path`, owner-only like the rest)

- `GET /api/threads?channel=<id>` rows gain `state`, `state_by`, `state_reason`, `state_at`.
- `GET /api/needs` -> `{"threads": [...]}`: every thread whose state is `needs_you` or `waiting`, each row as the threads route gives it plus `channel` and `channel_name`, `needs_you` first, then newest first. Cap 200.
- `POST /api/threads/<channel>/<thread_key>/state` body `{"state": "done" | "needs_you" | "waiting"}` -> the row; `by` becomes `owner`. Only those three are settable. Through the relay this is a chat action, allowed for an approved device (it changes no mail and sends nothing).
- SSE event `thread_state`: `{"channel", "thread_key", "state", "by", "reason", "at"}` on every change.

## The app

- **Sticker** on each thread card: top-right beside the time. `needs_you` in the accent colour with weight, `waiting` in the warn colour, `working` muted with a small pulse (none under reduced motion), `done` muted with a check. `none` shows nothing. The reason is the card's `title` attribute and is read by screen readers.
- **Needs you** at the top of the channel list, above the DM, with a count badge of `needs_you` threads; tapping it shows the `/api/needs` list, each card carrying its channel's name under the title; opening a card goes to that thread in its channel (the hash route).
- **Filter chips** at the top of a channel's thread list: All, Needs you, Waiting, Done. The choice is per browser (localStorage), default All.
- **Override**: in the thread header a small button "Mark done" (or "Needs me" when the state is done); long-press on a card does the same on a phone. It calls the state route; the sticker updates from the SSE event.
- The app keeps every id and class the tests pin; new strings are plain words; the line cap in `test_the_app_stays_small` rises with a reason line.

## Out of scope for v0

Writing back to the ledger (an owner's "Done" does not close a ledger
row; a later version proposes a closure). Zulip gets no stickers; in
shadow mode the state lives in the web store only.
