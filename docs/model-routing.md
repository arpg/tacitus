# Model routing: one table, task by task

- **Status:** design (2026-10-31). Nothing here is built; there is no UI
  and none is proposed yet. It describes one owner-private table that
  says, for each task, whether a local model or a hosted one does it, and
  the floors no entry can lower.
- **Asked for by:** the user who asked for the macOS native backend
  ([macos-native-accounts.md](macos-native-accounts.md)): with mail and
  calendars kept on the Mac, the next question is which model reads them.
- **Builds on:** the remote layer's switches ([llm-lanes.md](llm-lanes.md)
  section 1), the chat tiers (`model_tiers.py`, llm-lanes section 2.16)
  and the stakes rule.

## Words

- **local**: the OpenAI-compatible endpoint in `<private dir>/remote-llm.json`
  (`remote_llm.py`), the `local` tier in chat. "Local" means the owner
  chose where it runs: on the Mac itself, on the owner's own server, or
  on a colleague's. The endpoint must be https; a model on the same Mac
  sits behind a TLS proxy (`tailscale serve`, Caddy).
- **hosted**: Claude through Claude Code, on the `opus` or `fable` tier.
- **task**: one kind of model work with its own inputs, for example
  "write an email body" or "read a new message into a record".

## What can be local today

Every row is a switch that exists now (`remote_llm.py switches`, the
`tiers` block). "Local" in the third column means the local model does
the work when the switch is set; "hosted" means Claude does it.

| Task | Today's switch | Local today? | What the local model sees |
|---|---|---|---|
| Read each new message into a record (class, ask, deadline, people) | `mail` = enforce, ghost or sealed | yes | every new message |
| Thread briefs on ledger rows | `mail` = sealed (built, not the plan) | yes | each open row's thread |
| Mail tagging (Outlook categories) | follows `mail` | yes | each new message |
| Email bodies in the user's voice | `drafting` = remote | yes; the `email-author` subagent (hosted) is the fallback and writes letters | the brief, the thread, the draft |
| List and admissions digests | `digests` = remote | yes | the digest mail |
| Morning briefing, afternoon update | `briefings` = remote | yes; hard items are handed to Claude one thread each | ledger, records, calendar |
| The briefing's pre-send "still open?" check | `briefing_audit` = claude or remote | yes, when set to remote | evidence with kept mail removed |
| Sunday audit | `audits` = remote | yes; flagged items go to Claude | ledger, briefing JSON |
| Scheduling turns (channel and proactive) | `scheduling` = shadow or enforce; `tiers.lanes` sched-channel = local | yes; every write passes the Claude backstop | the thread, solver output |
| Chat turns in a topic or lane | `tiers` (`@local`, `use local`, `tiers.lanes`) | yes, except high-stakes messages | the topic's transcript |
| Nightly ledger reconcile | `reconcile` = nightly | **no**: one Claude call per project | the local model's records |
| The scheduling backstop | none | **no**: it is Claude by design | the local model's writes |
| Hand-offs from the briefings and the audit | none | **no**: those are the items the local model passed up | one thread each |
| Letters | none | **no**: the `email-author` subagent | the request |

## The routing table

One owner-private file, `<private dir>/routing.json` (0600; the
resident cannot read or write it, as for `remote-llm.json`):

```json
{
  "default": "hosted",
  "tasks": {
    "mail.read":        "local",
    "mail.tag":         "local",
    "mail.draft":       "local",
    "digests":          "local",
    "briefings":        "local",
    "briefing.check":   "hosted",
    "audit.weekly":     "hosted",
    "ledger.reconcile": "hosted",
    "scheduling":       "local",
    "chat":             "hosted",
    "chat.room":        "hosted",
    "chat.dm":          "hosted",
    "letters":          "hosted"
  },
  "floors": {
    "stakes": "hosted",
    "keep_from_anthropic": "local"
  }
}
```

- **Values** are `local` or `hosted`; a task not listed takes `default`.
  `local` on an install with no endpoint reads `hosted`, as `@local` does
  today ("no local model set up" in the footer).
- **The table compiles to today's switches.** `remote_llm.py routing
  apply` would write `mail`, `drafting`, `digests`, `briefings`,
  `briefing_audit`, `audits`, `scheduling` and `tiers.lanes` from it, and
  `routing show` would print the table beside the switches it produced.
  No lane reads the table itself at first, so no code path changes; the
  switches stay the single source each lane reads.
- **Mapping:** `mail.read` and `mail.tag` → `mail` (`enforce` when local,
  `off` when hosted); `mail.draft` → `drafting` (`remote` / `off`);
  `digests` → `digests`; `briefings` → `briefings`; `briefing.check` →
  `briefing_audit` (`remote` / `claude`); `audit.weekly` → `audits`;
  `scheduling` → `scheduling` (`enforce` / `off`) and `tiers.lanes`
  sched-channel; `chat`, `chat.room`, `chat.dm` → `tiers.default` and
  `tiers.lanes` main, room, dm (`local` / `opus`).
- **Tasks with no local path** (`ledger.reconcile`, `letters`, the
  backstop, the hand-offs) accept only `hosted`; `routing apply` refuses
  `local` there with the reason, rather than writing a switch that does
  not exist.
- **The ghost-week gates stay.** `mail sealed` and `scheduling enforce`
  still need the ghost week's GO; `routing apply` reports a refused switch
  as the remote layer does today.

## The floors

Two rules sit under every entry. No table value lowers them.

1. **The stakes floor (`stakes: hosted`).** The autonomy rails as a
   routing rule, as `model_tiers.STAKES_RE` applies them to chat today:
   an item that names money, a contract, a dean, a program officer, a
   decline, tenure, personnel or a complaint is never decided by the
   local model, whatever its task's entry says. Today this holds for chat
   turns only; the table extends it to every task: a briefing item, a
   draft and a scheduling turn that match go to the hosted model. The
   user's own `@local` tag stays the one exception, as now.
2. **The keep floor (`keep_from_anthropic: local`).** Mail from the
   domains and addresses on the keep list, and admissions mail when that
   is on, never reaches the hosted model, whatever its task's entry says.
   This is today's `keep_from_anthropic` list, applied per task instead of
   only by the mail gate.

**When both apply** (a contract question from a kept domain), neither
model decides. The local model writes its record, and the item goes to
the user as a decision with that record only, the way a kept message that
asks for something becomes a `kept-` ledger row today. The user answers
it or tags it `@local` or `@opus` to say who may.

## Visible, and checked

- The doctor's "remote LLM" row gains the table: each task, its value,
  and any floor that moved an item this week.
- The 17:30 report's tier line gains one line per task: local and hosted
  counts and the floor moves, from the existing `tier-turns.jsonl` and
  `remote-llm/calls.jsonl` (sizes and timings only, never text).
- A test checks that `routing apply` writes exactly the documented
  switches, refuses `local` for the hosted-only tasks, and that every
  floor holds on a fixture item for each task.

## What a per-task toggle adds over today

- **One place.** Today the choice is spread over nine switches in
  `remote-llm.json` and the `tiers` block of `ea-config.json`; the table
  names tasks the way the owner thinks of them.
- **The stakes floor everywhere,** not only in chat.
- **The keep floor per task,** not only at the mail gate.
- **A default for new tasks:** a lane added later is `hosted` until the
  owner says otherwise.

What it does not add: a model choice inside a task (that stays the
switches and the tiers), or any change to what the mediator lets either
model do. The gates, the buttons and the mediator are the same for both.

## On a Mac

The `local` endpoint can be the Mac itself: an OpenAI-compatible server
(llama.cpp, Ollama or an MLX server) on loopback, behind `tailscale serve`
or Caddy for https, then `remote_llm.py init --endpoint https://...`. A
27B model at 4-bit quantization is about 15 GB of weights, so 32 GB or
more of unified memory. With the native backend
([macos-native-accounts.md](macos-native-accounts.md)) and every `local`
entry set, mail and calendars stay on the Mac and only the hosted tasks,
under the two floors, send text to Claude.
