## Closure signals: the label store and its evaluation

Two detectors decide when work is closed. The web chat's conversation
states (`thread_state.py`: rules first, then the local model) put a
sticker on each conversation. The ledger closure pipeline
(`closure.py`, `closure_scrubber.py`, `ledger_sync.py`, the briefings'
sweeps, `ledger_reconcile.py`) closes or proposes to close ledger rows.
Each place where someone says what state the work is in is a labelled
example for both. `preprocessor/closure_labels.py` keeps those examples
in one store. `preprocessor/closure_eval.py` scores the detectors
against them.

### What is labelled

| source | where it comes from | label | gold? |
|---|---|---|---|
| `owner-tap` | the owner's "Mark done" or "Needs me" in the web chat (`thread-state-labels.jsonl` beside the web store) | the state the owner set | yes |
| `card` | a card's status line in the web store: Sent, Created, Closed, Cancelled, Sent from Outlook, Revised, Kept open, a failure; a card copied from Zulip's past is read the way `thread_state.card_view` reads it | `done` when settled, `open` when it failed | no (the bot records it) |
| `ledger` | rows that reached `completed`, with the provenance `closure_metrics.py` gives and the closing signal `closure.py` recorded; reopens, rejected and confirmed close proposals (`ledger_events`); `item_feedback.py` dispositions | `done`, `open` or `waiting` | the owner's reopens, rejections, confirmations, dispositions and own-word closes |
| `owner-said` | `missed-closures.log`: a closure the owner had to state, with the log's autopsy (the signals present, fired and not evaluated) | `done` | yes: the detector missed it |
| `model` | the thread-state model's answers: its state and confidence at the time (the engine's observer), or its state alone from the store's event log for the days before the observer ran | the model's state | no: scored against later gold |

### The record

One JSON line per event in `<private dir>/evals/closure-labels.jsonl`
(mode 0600, in a 0700 directory):

```
{"key", "at", "source", "kind", "label", "subject_type", "channel_kind",
 "origin", "thread_ref", "ledger_ref", "features": {...}}
```

- `key` is a stable hash of the event. A key is written once, so
  `collect` can run any number of times, and the live observer and
  `collect` write one record for one tap.
- `label` is one of `done`, `open`, `needs_you`, `waiting`, `working`
  or `none`. The eval reads `done` and `none` as closed.
- `thread_ref` and `ledger_ref` are short hashes of the thread and of
  the ledger id. A thread key can be a topic name, so it never goes in
  as it is. A card's record takes the `thread_ref` of the conversation
  that asked for it (its pointer, or the card's own thread).
- `features` are what a detector can read when it decides: the message
  counts; the hours since the last owner, bot and any message; the last
  author; whether the thread ends on a question; a live card, the newest
  card's kind and outcome; a pointer to a card in the DM and that card's
  outcome; a stuck turn; what the rules say and which rule says it
  (`rule_state`, `rule_id`); the state and `by` the tap replaced; the
  linked ledger row's type, status and days to its deadline; the
  interaction type (`owner_ask`, `scheduling`, `room`, `mail_card`,
  `automated`, `briefing_reply`, `bot_post`, `mail_item`). The full list
  is `FEATURE_KEYS`.

No text goes in: no mail, no message, no name, no subject.
`closure_labels.validate()` refuses a record with an unknown key and
any string that is not a short token (no space, at most 40 characters
of `[A-Za-z0-9_.:+=/-]`). The subject of an owner's ask is a class
word (`scheduling`, `drafting_email`, `drafting_doc`, `instruction`,
`lookup`, `chit_chat`, `other`), read in memory from the first owner
message by the closure analysis's patterns.

### Who writes it

- The bot, at once. `web_thread_state.observe` is the observer of the
  thread-state engine: each model answer and each owner tap becomes a
  record with the thread as the rules read it then.
- The nightly collect, as the owner (docs/SETUP.md, "Cron schedule"):

  ```
  python3 /opt/ea-system/preprocessor/closure_labels.py collect --report
  ```

  It reads every source read-only (the ledger and the web store through
  `ledger.guarded_connect`, the logs in `~/.ea` through `resident_io`)
  and appends what the store does not hold yet. A tap the observer did
  not see is rebuilt from the store as the thread stood at the tap
  (`reconstructed`); its linked rows are read as they are now.
  `--report` then writes the evaluation. `collect --days N` reads only
  the last N days; `stats` counts the store by source and label.

### A tap writes back to the ledger

A closure the owner has to state is a detector defect, so a tap closes
the loop where that is safe:

- **Mark done** on a conversation whose linked rows are open:
  `ledger.py close --user-said "marked done in the web chat"
  --skip-archive` (no mail moves), then `log_missed_closure.py` with the
  same words and source `web-chat`.
- **Needs me** on a conversation whose row carries a close proposal:
  `ledger.py reject`. On a row a signal closed in the last 7 days (not
  the owner's own word, and not a duplicate fold): `ledger.py update
  --status open`, which records the reopen and vetoes the signal. A row
  the web chat's own Done closed opens again the same way. Each then
  gets `item_feedback.py --kind still_open`.

Nothing is written back for an automated post or a reply in one (a
briefing names many rows), for more than 3 rows, or for several open
rows on different mail threads. The record's `writeback` feature says
what the tap did (`close`, `reject`, `reopen`, `none`, `automated`,
`ambiguous`).

### Reading the evaluation

```
python3 /opt/ea-system/preprocessor/closure_eval.py report [--days 30] [--md|--json] [--no-write]
```

The report goes to `<private dir>/evals/closure-eval-latest.md` and
`.json`. The daily health report (`mail_privacy.py report --post`)
carries its one line: labels in the window, gold labels, owner taps,
and the F1 of the rules, the model and the ledger pipeline.

- **Thread-state rules** against the owner's taps, by interaction type
  and by rule id. A tap the rules left to the model counts as "not
  closed"; `covered` says how many taps a rule settled.
- **Thread-state model** against the same taps: its latest judgment on
  the thread before the tap, else the state it had set. Calibration
  buckets the confidence of its answers (below 0.6 included) against
  how often they were right.
- **Ledger pipeline**: caught (a rule or model close, or a confirmed
  proposal), missed (the owner's word), wrong (a close the owner
  reopened, a proposal the owner rejected), by ledger type and by
  signal, with the close precision and the proposal precision of each
  signal. Plugin rows are left out. The share of stated closures where
  no signal fired is the scrubber's backlog in one number.
- **Top disagreements**: (who, interaction type, rule id, predicted,
  gold) and how often.
- **Candidate rules**: each backtested over the recorded features. It
  shows where the rule fires on every chat record, its precision and
  recall on the taps, and its agreement with the model. The rules are:
  a card's status line, a stuck turn, a pointer to a settled card, and
  a linked row completed. 48 hours of silence is the negative example:
  the closure analysis found it wrong for 38% of the threads that end
  on the bot's prose.

Limits: the taps are few until the web chat has been in use for some
weeks; a reconstructed tap reads its linked rows and a copied card's
text as they are now; a card's status time is the row's last change.
