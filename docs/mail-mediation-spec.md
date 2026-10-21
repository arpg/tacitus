# Mail-op mediation: remove send + raw-token access from the resident

**Status:** LIVE (2026-07-13). All three phases complete; see Implementation status at the end.

## Goal
Make it *structurally* true that the resident EA (the `ea-bot` user — the prompt-injectable
process that reads untrusted email/Slack) can neither (a) send mail or calendar invites on
the owner's behalf, nor (b) read the raw Graph/Google tokens. Read + draft keep working for the
resident. **No new OIT app registration.**

## Why (current state)
- One shared credential set in `~/.ea/auth`, group-`ea` readable → `ea-bot` reads it.
- Outlook token rides **Microsoft Graph PowerShell** (client `14d82eec…`), so it carries
  `Mail.Send` plus a tenant-admin-grade delegated scope set (bounded by the owner's own directory
  role, but `Mail.Send` / `Mail.ReadWrite` / `Files.Read` / `Contacts.Read` / `Calendars.ReadWrite`
  are fully live).
- `gmail_client` token has `gmail.send`; the calendar token has `calendar` (write). All ea-bot-readable.
- `send-draft` is gated only at the `graph_actions` front door (one-time confirm-token). A
  prompt-injected resident can bypass it by reading the raw token and POSTing Graph `/sendMail`.
  So "the owner is the only sender" is convention, **not enforced**.

## Design

### A. Token custody (the actual boundary)
Move every send/mutate-capable credential to **owner-only `0600`** (owner-owned, no group read):
`msal_token_cache.json`, `google_token_*_gmail.json`, `google_token_*.json`, `google_client_secret.json`.
After this ea-bot cannot read any token → cannot call Graph/Google directly at all.

### B. Mediator (owner-side executor): recommended, a thread in the existing bot
The bot (`ea-chat-bot.py`) already runs as the owner, holds the token, and performs the gated sends,
so add a watcher **thread** (like `resident_router_loop`) — no new service to run or monitor.
- Request: ea-bot writes `~/.ea/state/mail-queue/req-<id>.json = {verb, args, ts}` (state/ is group-writable).
- Executor: validate → run the real client as the owner → write `resp-<id>.json = {stdout, stderr, rc}` (g+r).
- Only **SAFE** verbs are executed; everything else is default-denied.

### C. Transparent forwarding (the resident keeps its current calls)
`graph_actions.py` (+ `gmail_client` / `google_calendar_client`), when it detects it can't read the
token (i.e. it's ea-bot), routes by verb — same pattern as the `slack_upload` auto-stage we shipped:
- SAFE verb → write request, poll for response (~30s timeout), return it. The resident's existing
  `graph_actions.py search-mail …` / `create-reply …` calls work unchanged.
- SEND/gated verb → refuse with the current message ("create the draft; it posts a Send button").
  Now unbypassable, because there is no token to fall back to.

### D. Send / calendar-mutate path (unchanged, now the ONLY one)
Button click → bot (the owner) mints confirm-token → `send-draft` as the owner, direct. Calendar
create/update/delete stay on the existing `pending-event.json` → button flow (owner-executed).
ea-bot has no route to any of these.

### Verb classification (validated against a month of production logs — see below)
**SAFE (mediated for ea-bot)** — the live `mail_mediation.SAFE` sets:
- graph_actions read: `search-mail`, `list-mail`, `get-message`, `get-event`, `calendar-view`,
  `resolve-contact`, `get-attachments`, `download-attachment`, `scan-attachments`,
  `get-gmail-draft` (an owner plugin's verb, such as `get-teams-dms`, is
  classified from the owner's plugin lock instead; docs/plugins.md)
- graph_actions draft: `create-draft`, `create-reply`, `update-draft`, `delete-draft`,
  `preflight-check`, `attach-file`
- graph_actions reversible inbox state: `archive-message`, `archive-conversation`,
  `snooze-message`, `wake-snoozed`, `create-todo`
- `gmail_client.py`: `search`, `get-message`, `get-thread`, `get-draft`, `create-draft`,
  `update-draft`, `delete-draft`, `list-attachments`, `download-attachment`, `list-drafts`
  (ids and times of the newest drafts; how the bot finds a draft the Gmail MCP created)
- `google_calendar_client.py`: `get-events`, `list-ea-managed`. The second is a read of the
  EA's own rule calendar, added 2026-08-28: without it the un-gated EA-Managed workflow
  (list the holds, then move or delete one) was refused at the shim as an unknown verb.
- `scheduling_engine.py`: `propose-meeting`, `validate-draft`, `log-decision`, `review-patterns`.
  The solver only READS calendars (and writes a local decisions file), so it is mediated to let
  the token-less resident run it. This set is EXACTLY the four subparsers argparse defines:
  `find-slots` and `check-slots` sat here until 2026-08-22 and matched no subcommand at all, so a
  forwarded call reached the mediator and died in argparse with exit 2 (B2-19).
- `deck_serve.py`: `serve`, `off`, `status`. The tailnet route for an EA-built deck. The
  route is a tailscaled write only the operator (the owner) may make, so it is executed owner-side;
  deck_serve.py re-validates there (plain slug, EA port range 3050-3069, the target port's
  LISTEN socket owned by ea-bot per /proc/net/tcp, no takeover of another user's path, no
  Funnel enabled on the node). There is no funnel verb, so a mediated route is tailnet-only
  by construction. See SECURITY.md Layer 5, "Deck hosting rides the same boundary".

**GATED (owner-only, button; NEVER mediated):**
- `graph_actions`: `send-draft`, `send-gmail-draft`, `schedule-send`, `request-confirm`
- `graph_actions`: `create-event`, `update-event`, `delete-event`
- `gmail_client.py`: `send-draft`   ← Gmail has its own send path, separate from graph_actions
- `google_calendar_client.py`: `create-event`, `update-event`, `delete-event` (owner-side they
  write the account's own calendar only with a button token; a request carrying
  `--confirm-token` is never part of the carve-out below)

**One scoped exception to the last line** (`is_ea_managed_autohold`, added 2026-08-22): a
`google_calendar_client.py create-event` that names `--calendar-id <EA-Managed>` and carries no
`--attendees` / `--attendee` runs un-gated. That calendar is the assistant's own rule store —
attendee-less holds and the `wh`/`nm`/`p0`/`p1`/`p2`/`wr` preference tokens — so nothing on it
mails anyone. `is_allowed()` = SAFE ∪ autohold, and the mediator calls that same function
server-side, so the carve-out is enforced at the trust boundary rather than in the shim only.
The mediator's call passes `owner=True`, so it compares against the id in the owner-private
`calendars.json` only, and its child gets `EA_CALENDAR_OWNER_SIDE=1`, so the client reads the
same file; the resident's `scheduling-preferences.json` is a hint for the shim's pre-check.
Owner-side, a move or delete there also needs the event to have no attendees and EA-Managed as
its organizer.

**Unknown verbs:** the resident sometimes *guesses* verb names (`list-drafts`, `list-events`,
`list-attachments` show up in the logs but don't exist). The mediator's default-deny must return a
clear "unknown verb" error, not a silent failure, so the resident self-corrects (it already does).

### Security review (the part that matters)
The mediator runs **ea-bot-supplied args as the owner**, so it must be bulletproof:
- **Default-deny verb allowlist** (anything not SAFE is refused).
- **No shell** — exec via argv list, never a string.
- **Path confinement:** every file arg (`FILE_FLAGS` = `--output`, `--body-file`, `--path`,
  `--file`, `--screenshot`, `--attachment`, `--out`, `--dest`, `--input`) must resolve *inside*
  ea-bot-owned dirs (`~/.ea/uploads`, `~/.ea/receipts`, `~/.ea/docs`, `~/.ea/state`, `/tmp`); reject anything
  resolving into `/home/<you>`, `~/.ea/auth`, the source tree, or system dirs, and reject a symlink
  whose real target leaves those roots. This stops an owner-run op from overwriting owner/root files.
  **Implemented for BOTH argv spellings** (`--body-file /path` and `--body-file=/path`):
  `_file_args()` matches the bare token and the `=` form. Until 2026-08-22 it matched only the
  bare token, so the `=` form skipped confinement on both sides of the boundary: the owner-side mediator
  re-runs this same function, so an attached `=` value was executed as the owner with no path check at
  all (audit B2-01, fixed).
- **Exact option spellings:** `FILE_FLAGS` match by spelling, so every mediated parser and
  subparser sets `allow_abbrev=False`, and `sanitize()` first refuses any option token that is
  not an exact option of the verb's parser (`--body-f`, `--outp=/x`, `--`, an option of another
  verb), and a short or alias spelling of a file option. The option set is read from the source of
  the tool the mediator runs (`mail_mediation.parser_options`); a test checks it against the parser
  argparse builds. A plugin verb's parser is not readable, so a strict prefix of a file flag is
  refused there. A second use of an option is refused unless the verb's parser appends it
  (`mail_mediation.repeatable_options`): argparse keeps the last use, and the carve-out check
  must read the value the child acts on. Refused, never rewritten.
- **Unit sandbox:** `ProtectSystem=strict`, `ProtectHome=read-only`, writes only to ea-bot's home,
  `/tmp`, the owner-private directory and the staging root (`deploy/mediator-sandbox.sh`, used by
  `enable-mail-mediation.sh` and the `harden-permissions.sh` drop-in). No `NoNewPrivileges=`: the
  resident-side file copies run through `sudo -u ea-bot`.
- **Known open (audit B2-03):** the check and the child's `open()` are separate moments. A writer
  in a shared ea-bot-writable root (`~/.ea/state`, `/tmp`) can swap a validated path for a symlink
  in between — a TOCTOU race on mediated output paths. Not fixed.
- **Dispatch attribution:** the request carries `run_id` (from `EA_RUN_ID`). The mediator's child
  runs as the owner with a fresh env, so without this every mediated draft mutation lands in the
  draft-events journal unlabelled (B1-05). The value crosses the trust boundary from ea-bot, so
  the mediator caps it at 64 chars and requires `[A-Za-z0-9._-]` before putting it in the child's
  environment; anything else means the variable is dropped, not passed through.
- IDs (message/draft/event/conversation) pass through (Graph-bounded).
- Structured audit log of every mediated call; simple rate/size caps.

This mediator is the new, small, default-deny trust boundary — strictly **less** ea-bot power than
today's "ea-bot holds a Graph-PowerShell token." Recommended over a `sudo -u <you>` wrapper precisely
because the thread gives ea-bot **no exec-as-owner path at all** (it only drops request files).

### Notes / loose threads
- **Gmail MCP** (`create_draft`) is a *separate* auth channel (claude.ai connector, not `~/.ea/auth`).
  The MCP toolset is draft-only (no send tool), so it doesn't open a send path — but Phase 2 should
  confirm the resident's Gmail MCP allowlist has no send tool.
- Google client tokens are per-account (`<your gmail>`, `<someone>@example.com`), move all.

## Empirical validation (cross-referenced against production logs)
Verified against **96 ea-bot transcripts (202 MB, 2026-06-13 → 2026-07-12)** plus the agent logs —
every verb the resident and the scheduled agents actually invoked was tallied.

**graph_actions call volume:** search-mail (306), get-message (163), create-reply (146),
create-draft (113), update-draft (26), download-attachment (26), resolve-contact (22),
get-attachments (19), attach-file (19), create-event (17), schedule-send (15), update-event (14),
delete-event (14), scan-attachments (13), send-gmail-draft (12), send-draft (12), request-confirm (12),
archive-message (10), preflight-check (7), delete-draft (7), get-event (4), snooze-message (3),
wake-snoozed (1). Nothing outside the classification.

**gmail_client.py is a first-class, heavy call site** (not just the Gmail MCP): search (77),
create-draft (47), get-message (18), update-draft (15). It has its own `send-draft`, so **Google
must be locked in the same pass** — Outlook-only would leave a live `gmail.send` path wide open.
**google_calendar_client.py:** get-events (25) + create-event (5).

**Call-site map — who reads the token, as which user, and what happens after the move:**

| Caller | User | After token → owner-only |
|--------|------|------------------------|
| Resident pane (interactive) | ea-bot | **mediated** |
| Scheduled agents: morning-briefing, afternoon-catchup, weekly-audit, list-digest, scheduling-check, priority-check (`run-agent.sh` → `sudo -u ea-bot claude`) | ea-bot | **also mediated** (same forward path) |
| `scheduled_sends.py check` (*/5 min, SENDS due mail) | **owner** | unaffected; keeps the send token |
| `ledger_sync.py check` (*/15 min, reads inbox to auto-close) | **owner** | unaffected |
| Bot button-send (`request-confirm` → `send-draft`) | **owner** | unaffected |

The load-bearing insight: **the send-capable background job (`scheduled_sends`) already runs as the owner**,
as do `ledger_sync` and the button flow, so moving the token to owner-only does *not* break scheduled
sends, auto-close, or approved sends. Only the ea-bot processes (resident + the scheduled Claude
agents) route through the mediator.

That job running as the owner is *necessary* but was never *sufficient*: its queue file
(`~/.ea/state/scheduled-sends.json`) lives in a group-writable state dir, so ea-bot can append to
it. The original marker was the literal string `pre-approved:<anything>`, which meant any writer of
that file could send mail as the user with no Slack button (audit B1-01 / B2-02). Since 2026-08-22
each queue entry carries an **HMAC-SHA256 over `(draft_id, send_after, fingerprint)`** (payload
version 2; the fingerprint is the reviewed content, `draft_fingerprint.py`, and a version 1 entry
is dropped), signed by
`scheduled_sends.make_send_token()` with `~/.ea/auth/scheduled-send-hmac.key`, an owner-only `0600`
file at least 32 bytes long. `cmd_check` re-verifies that signature before it touches the Graph
token, and `graph_client._validate_library_token` re-verifies it again on the library call.
Verification is **fail-closed**: a missing, unreadable or short key means nothing sends (an
unreadable key HOLDS the entry and alarms on Slack; a bad signature DROPS it and alarms). An entry
ea-bot writes, edits to fire early, or copies onto another draft no longer verifies, and a draft
whose content changed after the click (a SAFE `update-draft`, `attach-file` or
`remove-attachment`) is dropped with an alarm because its fingerprint no longer matches.

The signing key must be provisioned before deferred sends work at all; see SETUP.md "The deferred-send signing key".

**New coupling:** once ea-bot holds no token, its graph calls depend on the mediator being up. As a
bot thread that's just the bot's own (sentinel-watched) uptime; if we want the scheduled agents to
keep working while the bot is down, the mediator should be a small standalone owner-side service instead;
this is the real weight behind decision #1.

**No evidence of abuse:** the 6 `msal_token_cache` hits in the transcripts are `ls ~/.ea/auth`
directory listings, not reads of the token secret. The capability exists; it hasn't been used.

## Rollout (phased, reversible)
1. Build mediator thread + transparent forward + arg sanitizer + tests. Deploy **without** moving
   tokens (forward path inert; direct path still used). Exercise the mediator by forcing the queue path.
2. `chmod 0600` + `chown <you>` the tokens. ea-bot's direct path now fails → transparent forward
   activates. Verify the resident still searches/reads/drafts; verify it **cannot** send.
3. Red-team as ea-bot: tokens unreadable, raw Graph `/sendMail` fails, mediator refuses send,
   `--output /home/<you>/.ssh/authorized_keys` rejected, non-allowlisted verb rejected.
Rollback at any step: `chmod g+r` the tokens → direct path restored.

## Testing
- Unit: allowlist (send/gated refused), arg sanitization (path escapes + owner home/auth/source dirs blocked),
  request/response plumbing.
- Integration: ea-bot forward → mediator → response for `search-mail` and `create-reply`; `send-draft` refused.
- Red-team: the three attacks in Rollout step 3.

## Decisions (settled 2026-07-12)
1. **Standalone owner-side service** (`ea-mail-mediator.service`), not a bot thread, so the five
   scheduled Claude agents keep working even if the Slack bot is down.
2. **Google + Outlook in the same pass** (logs: `gmail_client` is 157+ calls with a live `gmail.send`).
3. **Calendar `create/update/delete-event` stay gated**, like send.

## Implementation status
- **Phase 1 (DONE, c7a0dab):** `mail_mediation.py` (shim + classification + path-confined sanitizer),
  `mail_mediator.py` (the owner-side service), the three clients wired to forward, systemd unit, tests.
  Deployed **inert** behind `mail_mediation.enabled` (default false).
- **Phase 2 (DONE, live 2026-07-13):** `mail_mediation.enabled=true`; all tokens in `~/.ea/auth`
  moved to owner-only `0600`; `ea-mail-mediator.service` active. Two fixes were needed to get here: the
  shim must run **before** `import graph_client` (which reads the token at module load, 96fd210), and
  the enabler's `send_refused` check must capture-then-grep, not pipe under `set -o pipefail` (a correct
  refusal exits 2, which pipefail was reading as failure, 5c88ab5).
- **Not a phase anymore (install integration):** the "phases" above were the one-time *migration* of an
  already-running deployment (verify the forward path while the token is still readable, then cut it,
  with auto-rollback). On a fresh install there is nothing to migrate, so mediation is now part of the
  mandatory two-user hardening: `scripts/install/setup-ea-user.sh` calls `deploy/enable-mail-mediation.sh`, which is a
  portable, idempotent enabler (installs the mediator unit for the invoking user, sets the flag, moves
  the tokens) that doubles as the standalone migrator/ops tool (`--status`, `--rollback`).
  The tokens are locked before any other step, and neither a failed check nor `--rollback`
  gives one back to ea-bot: they stay owner-only and the resident has no mail until
  mediation works again (the "chmod g+r" rollback above was the migration's, not the
  enabler's).
- **Phase 3 (DONE, all pass 2026-07-13):** red-teamed as ea-bot — tokens unreadable (6/6),
  `import graph_client` blocked at the token read, send-draft refused at the shim **and** server-side
  (raw queue injection bypassing the shim), `--body-file` path escapes + symlink→`/etc/passwd`
  rejected, SAFE reads/drafts still work via the mediator.

**Net result:** the resident (ea-bot) is now structurally unable to send mail or read the raw Graph/
Google tokens. Read/draft/reversible-inbox ops are forwarded to the owner-side `ea-mail-mediator` service;
send + calendar-mutate are refused for ea-bot and run only owner-side after a Slack button (unchanged).
