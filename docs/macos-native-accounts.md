# macOS native accounts

- **Status:** design, with a prototype tested on Linux against a fake
  helper (2026-10-31). Nothing here has run on a Mac yet. "First tests on
  a Mac" below is the order to try it in. Nothing is wired into
  `graph_actions.py` yet ("Rollout").
- **Asked for by:** a Mac user who wants Tacitus to use the accounts the
  Mac already has, the way Mail and Calendar do, instead of an Azure app
  registration and a Google Cloud OAuth project.
- **Code:** `preprocessor/native_accounts.py` (the protocol, the clients,
  the scope, the doctor rows), `preprocessor/availability.py` (other
  people's free/busy), `deploy/macos/tacitus-native/` (the Swift helper,
  the Mail script, the Info.plist, the build script, the launch agent),
  `tests/test_native_accounts.py`, `tests/test_availability.py`.
- **Companion:** [model-routing.md](model-routing.md), the per-task
  local or hosted model table the same user asked for.

## In one paragraph

The owner signs in to work Exchange, Google and iCloud once, in System
Settings > Internet Accounts. Mail and Calendar then use those accounts
with no registration anywhere. The native backend lets Tacitus do the
same: EventKit for every calendar, Mail.app scripting for mail, and the
Contacts framework for people. There is no app registration, no OAuth
token on disk, and no token for the mediator to guard: the owner's login
session is the credential. The trust model does not change. The resident
still calls `graph_actions.py`, the mediator still decides SAFE or GATED,
and a send still needs the owner's click. Only the client under the
mediator changes. Other people's free/busy is a separate choice, the
**availability provider**, because nothing on the Mac can read it.

## The three settings

| Key (`ea-config.json`) | Values | Default | What it picks |
|---|---|---|---|
| `mail.backend` | `graph`, `gmail`, `macos` | `graph` | Where mail is read and drafted |
| `calendar.backend` | `graph`, `google`, `macos` | `graph` | Where the owner's calendars are read and written |
| `availability.provider` | `graph`, `owa`, `none` | `graph` | Whose free/busy the solver can read for other people |

`graph` keeps today's behaviour (Outlook through Microsoft Graph, with
the Gmail and Google accounts beside it). `ea-config.json` is the
resident's to write, so on a Mac the owner-private
`~/.ea-private/native-accounts.json` (its `backend` block) outranks it for
owner processes (`native_accounts.backends`). None of the three values
widens what the resident may do: the verb gate is the same on every
backend.

**Recommended Mac default:** `mail.backend` `macos`, `calendar.backend`
`macos`, `availability.provider` `owa`. The long-term path for free/busy,
and for registration-free Graph and Google on every platform, is one
Tacitus app per provider ("The long-term path").

## What Apple gives a local app, and what it does not

The request named facts to build on. Each was checked against Apple's
documentation; two do not hold (rows 4 and 5).

| # | Fact | Source |
|---|---|---|
| 1 | No API hands an app the credentials behind Internet Accounts. The Accounts framework, the one API that exposed account objects, is deprecated: "contact the provider of the service you integrate with". | [S1] |
| 2 | Internet Accounts turns each app on or off per account ("Turn on or off any feature you want to use with the account"). That is the owner's first filter on what Tacitus can see. | [S2] |
| 3 | EventKit reads and writes events on every calendar Calendar shows, behind one prompt. `requestFullAccessToEvents` (macOS 14) needs `NSCalendarsFullAccessUsageDescription`. Sources are `exchange`, `calDAV` (Google and iCloud), `local`, `subscribed`. | [S3] [S4] [S5] |
| 4 | **EventKit cannot invite.** `attendees` is read-only: "it is not possible to add attendees with Event Kit". `EKParticipant`: "EventKit cannot add participants to an event nor change participant information." The request's "write attendees, invitations" does not hold. | [S6] [S7] |
| 5 | **EventKit has no free/busy for other people and no RSVP.** `EKEventStore` has no such method; it searches only calendars on the device. | [S8] |
| 6 | Calendar.app's own scripting can add an attendee to an event. Apple's guide does not say whether Calendar then sends the invitation. | [S9] |
| 7 | A date-range query covers at most four years. An event's identifier "most likely changes" when its calendar changes; Exchange external identifiers differ between devices. | [S10] [S11] [S12] |
| 8 | Mail has no public framework. A local app drives it with Apple events, behind the Automation permission; `NSAppleEventsUsageDescription` is required, and Apple's own example is Mail's local database. | [S13] |
| 9 | JavaScript for Automation runs under `osascript` and filters Mail's messages with `whose` clauses (`Mail.inbox.messages.whose(...)`). | [S14] |
| 10 | `AEDeterminePermissionToAutomateTarget` checks the Automation permission without a prompt: `errAEEventWouldRequireUserConsent` when never asked, `errAEEventNotPermitted` when refused, `procNotFound` when Mail is not running. | [S15] |
| 11 | Reading `~/Library/Mail` directly needs Full Disk Access, a grant to all protected data at once. This design does not use it. | [S16] |
| 12 | Contacts asks once (`requestAccess`, `NSContactsUsageDescription`) and finds a contact by email address. | [S17] [S18] [S19] |
| 13 | A daemon "whose UID is set to that of the logged in console user is not equivalent to an application that has been launched by that user". A GUI launch agent runs in the user's Aqua session with all GUI services. | [S20] |
| 14 | A command-line tool carries its usage descriptions in an Info.plist linked into `__TEXT,__info_plist`; without them the prompts do not appear. The prompt names the responsible process, so a tool started from Terminal asks as Terminal. | [S21] [S22] |

## The shape

```
 resident (ea-bot)          owner, LaunchDaemon                 owner, login session (Aqua)
 ─────────────────          ───────────────────                 ───────────────────────────
 graph_actions.py           ea-mail-mediator                    tacitus-native serve
   search-mail ...  ─────►   SAFE / GATED check (unchanged)      (launch agent)
   (unchanged call)          runs graph_actions.py as owner        ├─ EventKit, Contacts in process
   ~/.ea/state/mail-queue    backend macos:                        └─ osascript -l JavaScript mail.js
                             native_accounts.NativeMailClient          └─ Mail.app (Apple events)
                               ─► ~/.ea-private/native-queue ─►
                                  req-<id>.json / resp-<id>.json
```

- **The resident's calls do not change.** It runs
  `graph_actions.py search-mail ...` as today. The mediator classifies the
  verb from `mail_mediation.SAFE` and `GATED` as today. The owner-side
  child picks its client from `mail.backend` and `calendar.backend`.
  `native_accounts.NATIVE_VERBS` maps each verb to a client method, and
  `verb_class()` reads its class from `mail_mediation`, so the native
  backend cannot reclassify a verb (a test checks it).
- **Why a launch agent.** EventKit, Contacts and Apple events to Mail need
  the owner's login session, and the permission prompts need a screen.
  The mediator is a LaunchDaemon in the system domain, which has neither
  [S20]. So the helper runs as the owner's own launch agent
  (`~/Library/LaunchAgents/org.ea-system.tacitus-native.plist`,
  `LimitLoadToSessionType` Aqua), and the mediator reaches it through
  request files in an owner-private directory. Both ends are the owner;
  no trust boundary is crossed there.
- **Why the agent's program is the Swift binary.** macOS ties each grant to
  the responsible process [S22]. The agent starts `tacitus-native`
  directly, so the grants name tacitus-native. Started through Python or
  a terminal, they would name Python or Terminal, and every script of the
  owner would share them. `mail.js` runs as the helper's child, so the
  Automation grant is tacitus-native's too (to verify on a Mac).
- **Swift where Apple has an API, JXA where it has not.** EventKit and
  Contacts are Swift frameworks. Mail has none [S13], so a fixed JXA
  script drives its scripting dictionary. The request is JSON on stdin;
  no script source is ever built from a request or from mail, and no verb
  runs a shell (a test checks the script for `doShellScript`, `eval` and
  `Function`).

### The protocol

One JSON object per request and per answer (`native_accounts.make_request`,
`parse_response`):

```json
{"v": 1, "id": "<32 hex>", "verb": "list-events",
 "args": {"calendars": ["<EventKit id>"], "start": "2026-10-27T00:00:00Z", "end": "2026-10-28T00:00:00Z"}}

{"v": 1, "id": "<same>", "ok": true,  "result": {"events": [...]}}
{"v": 1, "id": "<same>", "ok": false, "error": {"code": "permission-denied", "message": "calendar access is denied"}}
```

Error codes: `bad-request`, `unknown-verb`, `permission-denied`,
`not-determined`, `not-found`, `unsupported`, `mail-not-running`,
`out-of-scope`, `timeout`, `internal`, `protocol`. An answer for another
id, another version, or over 16 MB is a `protocol` fault.

| Helper verb | Where | Class |
|---|---|---|
| `status`, `request-access` | Swift | read, setup |
| `list-calendars`, `list-events`, `get-event` | Swift (EventKit) | read |
| `create-event`, `update-event`, `delete-event` | Swift (EventKit) | **gated** |
| `find-contacts` | Swift (Contacts) | read |
| `mail-status`, `list-mailboxes`, `list-messages`, `search-messages`, `get-message`, `find-thread` | mail.js | read |
| `save-draft`, `delete-draft` | mail.js | draft |
| `move-message`, `set-flags` | mail.js | inbox state |
| `send-message` | mail.js | **gated** |

`NativeHelper.call` refuses a gated verb unless it carries an approval
minted for that verb by the client method that owns the gate, and an
approval works once. So a code path that reaches the helper without
passing `send_draft`'s or `create_event`'s checks sends nothing.

Two transports: `QueueTransport` (the services; `req-<id>.json` in, the
agent renames it to claim it and writes `resp-<id>.json` through a
temporary name; the queue must be a real directory of the owner, mode
0700, on both sides) and `SubprocessTransport` (`tacitus-native call`, one
process per request, for an owner shell; its prompts would name Terminal).

### The trust model, checked

| Question | Answer |
|---|---|
| Can the resident read mail or calendars directly? | No. It holds no token, as today. The helper's queue is owner-only. Mail and the agent run in the owner's login session. Apple events from another user to the owner's Mail, and EventKit as ea-bot (its own empty store), are expected to fail; "First tests on a Mac" item 18 confirms it. |
| Can the resident send? | No. `send-draft` is GATED in the mediator (unchanged). Owner-side, `NativeMailClient.send_draft` makes graph_client's checks: a fingerprint, a valid one-time token bound to the draft and that fingerprint (`graph_client._validate_library_token`), the content unchanged, the draft's lock held, and not the resident's euid. Then Mail's copy of the draft is read again; an edit made in Mail after the card is a change, and the owner reviews it on a new card. |
| Can the resident widen what the EA sees? | No. The accounts, mailboxes and calendars come only from the owner-private scope file. A folder name outside it is refused (`out-of-scope`). |
| Can a request inject a script? | No. `mail.js` is a fixed file under `/usr/local/libexec/tacitus-native` (root-owned). The helper refuses to run it when another user can write it or its directory. Requests are data. |
| What is new? | The TCC grants belong to one binary. Whatever runs as `tacitus-native` reads the owner's mail, as whatever holds the Graph token does today. The binary is root-owned. An owner process can write the queue; owner processes already hold the tokens today. |

## The scope file

`~/.ea-private/native-accounts.json` (owner, 0600). The example is
`deploy/macos/tacitus-native/native-accounts.example.json`. Fill it from
what the Mac has:

```bash
python3 preprocessor/native_accounts.py accounts    # Mail accounts and their mailboxes
python3 preprocessor/native_accounts.py calendars   # every calendar EventKit sees, with ids
```

- `mail.accounts[]`: `name` (as Mail shows it), `kind` (`exchange`,
  `gmail`, `icloud`, `imap`: the default mailbox names), `addresses` (the
  owner's own; replies leave them out), `mailboxes` (the role names
  `inbox`, `sentitems`, `archive`, `drafts`, `deleteditems` when the
  defaults are wrong), `search` (the mailboxes a search reads).
  `search_days` bounds a search and a thread read; `search_body` lets a
  search read message bodies (slow on a large mailbox).
- `calendar.calendars[]`: `id` (EventKit's), `title`, `blocking` (a family
  calendar is context, not busy time), `write`. `default` is where an
  event goes; `ea_managed` is the EA's own rule calendar (the un-gated
  carve-out, as on Google). `shared` maps a colleague's address to a
  calendar of theirs the owner sees in Calendar (a delegate or shared
  calendar): free/busy for that person then comes from the Mac.
- `contacts.enabled`: the Contacts framework answers `resolve-contact`.

## Identities

| Graph | Native | Note |
|---|---|---|
| message `id` (changes on every folder move) | `nm_` + base64url of the RFC 5322 Message-ID | Stable across moves, so `ledger_sync`'s id healing never fires. The same Message-ID in two mailboxes (Gmail's INBOX and All Mail) is one message; the inbox copy wins. |
| `conversationId` | `nt_` + base64url of the thread's root Message-ID: the first id in References, else In-Reply-To, else the message's own | A reply that carries neither header starts its own thread. A thread read finds every message whose headers name the root (`find-thread`), then keeps those whose own root it is. |
| draft id | `nd_` + a random hex id, in the owner-private draft registry | Mail keeps no editable draft. An edit saves a new Mail draft and deletes the old one; the handle and the approval fingerprint stay with the registry. |
| event id | `ne_` + base64url of the EventKit identifier and, for an occurrence, its date | Every occurrence shares the identifier [S11]. |
| folder | a role (`inbox`, `sentitems`, ...) mapped per account | `parentFolderId` reads `<account>/<mailbox>`. |
| categories | none | Mail has no categories (see the table below). |

Every handle is a plain mail id (`mail_mediation.is_plain_id`), so the
mediator's id checks apply unchanged. A Message-ID longer than about 380
bytes has no handle (`ValueError`); such ids are rare.

## Verb mapping

Status: **built** (in the prototype, tested with the fake), **design**
(specified here, not built), **partial**, **gap** (no native equivalent).

| graph_actions verb | Class | Native operation | Status |
|---|---|---|---|
| `search-mail` | SAFE | `search-messages`: subject and sender, the body within `search_days`, over the scope's mailboxes | built; a scan in Mail, not a server index, so slower |
| `list-mail` | SAFE | `list-messages`, newest first, ids and dates in one Apple event per mailbox | built |
| `get-message` | SAFE | `get-message` by Message-ID in role order | built |
| `get-conversation` | SAFE | `find-thread` on the root Message-ID, then the root filter | built; quote stripping reuses graph_actions at wiring time |
| `create-draft` | SAFE | `save-draft` (new outgoing message, saved to the account's Drafts) | built (Mail behaviour to verify) |
| `create-reply` | SAFE | `save-draft` with Mail's `reply` command (threading headers), recipients set by the EA (reply-all minus the owner) | built (Mail behaviour to verify) |
| `update-draft` | SAFE | new Mail draft, old one deleted, handle kept | built |
| `delete-draft` | SAFE | `delete-draft` in the account's Drafts only | built |
| `preflight-check`, `draft-snapshot` | SAFE / bot | the registry entry is the draft's content | design (port `graph_client.preflight_check`'s rules) |
| `attach-file`, `remove-attachment` | SAFE | the registry's attachment list, then a re-save | design (Mail's attachment element to verify) |
| `get-attachments`, `download-attachment`, `scan-attachments` | SAFE | Mail's `mail attachment` element and `save` | design; an attachment Mail has not downloaded is a limit |
| `archive-message` | SAFE | `move-message` to the account's archive mailbox | built |
| `archive-conversation` | SAFE | one move per message of the thread | design |
| `mark-read` | SAFE | `set-flags` | helper built, client design |
| `set-categories` | SAFE | none on the server | **gap**: Mail has no categories. Options: Mail's seven flag colours, or EA labels in the owner-private store, visible only to Tacitus. The tagger's categories become local. |
| `snooze-message`, `wake-snoozed` | SAFE | Mail flag, move to archive, the same `snoozed-messages.json` | emulated: Mail flags carry no due date |
| `create-todo` | SAFE | EventKit reminders (a fourth prompt) | design, optional |
| `resolve-contact` | SAFE | Contacts framework (`native_accounts.resolve_contact`) | partial: the People API's ranking by recent contact is lost |
| `calendar-view` | SAFE | `list-events`, Graph's calendarView shape | built |
| `get-event` | SAFE | `get-event` | built |
| `clear-hold` | SAFE | `delete-event` on a hold: the `[EA Hold]` line in the notes stands in for the category | built |
| `send-draft` | GATED | `send-message` after the gate | built (Mail behaviour to verify) |
| `schedule-send` | GATED | `scheduled_sends.py` calls `send_draft` with its signed credential | design; the agent must be running at send time |
| `create-event` | GATED | `create-event`, attendee-less | built; **with attendees: gap** [S6] [S7]. Experiment: Calendar.app scripting [S9]. Until then the answer is `unsupported`, and the owner sends the invitation from Calendar or Outlook. |
| `update-event`, `delete-event` | GATED | EventKit `save`, `remove` | helper built, client design; whether the server notifies attendees when the organizer moves or deletes a meeting through EventKit is to verify |
| free/busy (getSchedule) | SAFE (solver) | the availability provider | see "Other people's free/busy" |
| mailbox settings, working hours | read | none | gap; the EA-Managed `wh` tokens are already the solver's source |
| Teams DMs plugin, Outlook-category tagger | owner | none | Graph-only |

`gmail_client.py` and `google_calendar_client.py` stay for a `gmail` or
`google` backend. On `macos`, a Gmail account in Mail is read like any
other account, and the EA-Managed calendar is whichever calendar the
scope names.

## What the closure scrubber and ledger_sync read

| Signal | Graph input today | Native input | Difference |
|---|---|---|---|
| `user_replied_outlook` | Sent Items (14 days) by `conversationId`; Archive as a second sent source (`_split_archive`) | each account's sent and archive mailboxes, joined on the thread root | Same logic. Sent mail from the phone appears after Mail syncs. |
| `user_replied_gmail` | Gmail `threadId` | the same thread root | Folds into the first signal: Gmail accounts in Mail join on References. |
| `inbound_ack`, `confirmation_email`, `project_member_completion` | inbox (14 days) | inbox mailboxes | Same fields. |
| `calendar_event_match` | calendarView (30 days) | EventKit | Same shape. |
| `meeting_response_sent` | "Accepted:" subjects in Sent | the owner's own participant status on the event (`isCurrentUser`, `participantStatus`) | Calendar answers an Exchange invite through the server and a Google invite through Google, so the answer may never be in Sent. EventKit's status is the better signal; design: synthesize the same records from events whose own status changed. |
| `ledger_sync` in inbox / archived | `parentFolderId` against the inbox id; re-resolve by `conversationId` on a 404 | `get-message` across roles: inbox, archive, not found (UNKNOWN) | Message-IDs do not change on a move, so a stale id is rare. `ea_moves` records the native client's moves the same way (wiring step). |

## Permissions and first run

Three prompts, each once, each naming tacitus-native:

1. **Calendars** (full access), from `requestFullAccessToEvents` [S3].
2. **Contacts**, when the scope enables it [S17].
3. **Automation of Mail**, from `AEDeterminePermissionToAutomateTarget`
   with a prompt [S15]. Mail must be open, or the answer is
   `procNotFound`.

```bash
sudo bash deploy/macos/tacitus-native/build.sh --install   # build, install, load the agent
python3 preprocessor/native_accounts.py request-access      # the three prompts, through the agent
python3 scripts/ea-doctor.py                                # the rows below
```

The doctor (`check_native_accounts`, `native_accounts.doctor_rows`) asks
the agent, so it reports the agent's grants, not the shell's:

| Row | PASS | WARN | FAIL |
|---|---|---|---|
| `native accounts (macOS)` | no backend is macos | | macos set on another OS |
| `native accounts: scope` | | | the scope file is missing or unreadable |
| `native accounts: helper` | the agent answers | | no answer: build, install, log in |
| `native accounts: availability provider` | graph or owa | none | |
| `native accounts: outlook-web-auth token` (owa) | the plugin holds a token | no token now | |
| `native accounts: <calendar, mail, contacts> permission` | authorized | not asked yet; Mail not running | denied (fix: the Privacy & Security pane) |
| `native accounts: Mail account <name>` | its role mailboxes exist | | not in Mail, or a mailbox is missing |
| `native accounts: calendars` | every scope calendar exists | | one is gone |

With mail and calendar on `macos` and availability `owa` or `none`,
`auth: graph and google tokens` reads "not needed".

The binary is signed ad hoc. A grant is tied to the code signature, so a
rebuild with changed code is expected to ask again (to verify); build once
per release.

## Other people's free/busy

The solver asks one question of other calendars:
`scheduling_engine._schedule_detail`, getSchedule's availability view per
address. It feeds `_get_internal_busy_slots` (Step 3 of
`_build_candidate_slots`, for `propose-meeting` and `reschedule-brief`)
and `check-attendees`. Nothing on a Mac answers it for a colleague [S8],
so it is its own seam, `availability.provider` (`preprocessor/availability.py`),
now wired into `_schedule_detail`:

| Provider | Reads | Needs | Notes |
|---|---|---|---|
| `graph` | Graph getSchedule (least privilege `Calendars.ReadBasic`; not for personal Microsoft accounts) [S23] | an app registration (SETUP.md Option A or B) or the institution's admin-consented app | today's behaviour, the default |
| `owa` | the same call on the token the [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth) owner plugin harvests from the owner's Outlook on the web session (SETUP.md "Option C: no registration (outlook-web-auth)") | that plugin, its first sign-in and its cron | no registration. The provider checks the plugin holds a token first and says so when it has not. `graph_client` asks plugin token sources after its own cache, so on a Mac with no registration this is the plugin's token. The plugin's README says the Outlook REST audience works for everything except flagged-message detection. |
| `none` | nobody | nothing | the degraded mode below |

**Under every provider, the Mac first.** With `calendar.backend` `macos`,
an address the scope maps to a calendar the owner sees (the owner's own,
or a shared calendar) answers from EventKit (`availability.LocalFirst`).

**The degraded mode (`none`).** Specified at the one consumer and built:

- `_schedule_detail` answers every address with an empty view and
  `NONE_REASON`; Graph is never called.
- `_get_internal_busy_slots` then marks each internal attendee
  `unchecked` and adds no busy time for them, as for any unreadable
  calendar. The options fit the owner's calendars, the EA-Managed tokens,
  and what the thread states.
- What the thread states still binds. Offered times (an attendee's
  `availability`), a booking link's slots (`availability_complete`),
  `excluded_days` and a stated timezone (Step 4) apply unchanged, so
  `none` loses nothing when the other side named times.
- The output says so: `"availability": {"provider": "none", "mode":
  "invitee-picks"}` on `propose-meeting` and `check-attendees`, each
  internal attendee `NOT CHECKED: ...` in `attendees_checked`, and one
  warning: "offer them and let <people> pick, or ask for a booking link"
  in place of "could not read the calendar".
- The reply offers two or three times (the operating file's rule for a
  yes to a meeting) and asks the invitee to pick or to send a booking
  link. An EA hold on each offered slot keeps it free until the answer
  (the existing hold path); `validate-draft` re-checks the owner's side
  when the answer comes.

**Generalising `owa` beyond one tenant.** The plugin is per user, not per
institution in its code; it signs in to whichever tenant the owner uses.
What a second tenant, or another institution, needs:

1. **One profile and token per account.** The harvest keeps one browser
   profile and one token file. An owner in two tenants needs one of each
   per account, an auth source that takes the account, and a provider that
   asks the tenant that holds the attendee (free/busy works within a
   tenant, and across organizations only where they federate it).
2. **Federated sign-in pages.** The headless run notices an expired
   session by the Microsoft sign-in hosts. A tenant that federates to its
   own identity provider sends the browser elsewhere, so the test should
   be "not on the Outlook host". The plugin's network grant names two
   hosts; such a tenant adds its identity provider's host, which the owner
   consents to on install.
3. **Other clouds.** Government and national clouds use other Outlook,
   sign-in and Graph hosts; they become settings.
4. **Tenant policy.** Conditional access (a compliant device, token
   binding) can refuse a headless browser, and continuous access
   evaluation shortens tokens. The plugin then cannot work there, and the
   README's rule stands: only where the organization's rules allow it.
5. **The token cache's format** in the browser is Microsoft's and can
   change; that risk is the same for every tenant.

## The long-term path: one Tacitus app per provider

Per-user registration is the install's heaviest step on every platform:
an IT request for an Azure app, or a Google Cloud project per user. The
lasting fix is that the Tacitus project registers once and every install
uses that registration:

- **Microsoft.** One multitenant Entra app ("Accounts in any
  organizational directory") published by the project, a **public
  client**: it runs on the user's machine and holds no secret ("they
  can't have client secrets") [S24]. Sign-in is the authorization-code
  flow with PKCE on a loopback redirect, or the device-code flow. Each
  institution's administrator consents once for the whole tenant
  (`prompt=consent`, or the admin consent URL); after that no user of that
  tenant is asked [S25]. The client id ships in the code as the default
  `GRAPH_CLIENT_ID`. Tokens stay in the owner-private MSAL cache on the
  user's machine; the project holds no token and runs no server.
- **Google.** One OAuth client of the desktop type, published by the
  project: a public client with PKCE and a loopback redirect [S26]. Gmail
  scopes are restricted, so the project passes Google's verification and
  an annual security assessment once, for every user [S27]. Calendar-only
  use needs the lighter sensitive-scope review.
- **On every platform.** SETUP.md Options A and B become "sign in"; the
  Google Cloud steps disappear. Linux, macOS and Docker installs use the
  same two client ids. Option C (outlook-web-auth) stays for a tenant
  whose administrator will not consent.
- **Beside the native backend.** The two compose. A Mac keeps mail and
  calendars native and asks the Tacitus app only for free/busy
  (`availability.provider` `graph` on the project's client id), which
  needs the smallest consent: `Calendars.ReadBasic` [S23]. An
  administrator who would not consent to mail access may consent to that.
  A Linux install uses the app for everything.
- **What the project takes on.** Owning the registrations, the Google
  verification and its renewal, and answering administrators' consent
  questions. Consent can be revoked per tenant at any time [S25].

## Limits

- Mail must be running, signed in and synced, and the owner logged in:
  the agent lives in the login session. After an unplanned reboot with
  FileVault, nothing runs until the owner unlocks (docs/macos-services.md).
- Speed: Mail answers `whose` clauses by scanning; a search, a thread read
  and a bulk list cost seconds on a large mailbox. To be measured.
- No server-side categories; snooze is a flag, a move and the local file.
- Sent-mail signals come from each account's sent mailbox after Mail syncs
  it; a meeting answer may not be in Sent at all (see the scrubber table).
- Bodies are plain text: Mail scripting writes text, so HTML formatting
  in a reply is Mail's own.
- No invitations through EventKit [S6] [S7]; no colleague's free/busy
  without a provider [S8].
- A rebuilt helper is expected to need new grants.

## Data sovereignty

On this backend mail and calendars never touch a token or a server that
Tacitus holds: they move between Mail, Calendar and the helper on the
Mac. What reaches a model is decided by the existing lanes. A local model
on Apple silicon serves the existing `local` tier: an OpenAI-compatible
server on the Mac behind https (the remote layer refuses http, so a TLS
proxy such as `tailscale serve` or Caddy fronts it), set up with
`remote_llm.py init`. A 27B model at 4-bit quantization is about 15 GB
of weights, so a Mac with 32 GB or more of unified memory. Which lanes
can be local, and the per-task routing table with its stakes and keep
floors, are in [model-routing.md](model-routing.md).

## Owner steps

1. `sudo bash deploy/macos/tacitus-native/build.sh --install` (one root
   step: build, install, load the agent).
2. Copy the example scope file to `~/.ea-private/native-accounts.json`
   and fill it from `native_accounts.py accounts` and `calendars`.
3. `python3 preprocessor/native_accounts.py request-access`: three
   prompts.
4. For `owa`: install outlook-web-auth (SETUP.md Option C, four steps).

Today's path on the same Mac: an IT request for an app registration and
its secret (Option A), or the plugin anyway (Option C), plus a Google
Cloud project with its consent screen and test users per Google account.

## Rollout

1. **This branch:** the protocol, the clients, the fake, the helper source,
   the availability seam (wired into the solver; the default `graph` is
   today's call), the doctor rows, the config keys. Nothing else reads
   the native clients.
2. **On a Mac:** "First tests on a Mac", fixing the helper and `mail.js`
   where Mail differs from its dictionary.
3. **graph_actions:** pick the client at import
   (`native_accounts.mail_client()` when `mail.backend` is macos), give the
   commands that call `graph.graph_get`, `graph_post` or `graph_patch`
   directly (snooze, categories, folders, attachments, preflight) a native
   branch, and keep their JSON shapes.
4. **The readers:** `preprocess.py`, `closure_scrubber.cmd_check`,
   `ledger_sync` and `scheduling_engine._fetch_all_events` take their
   lists from the native clients; the solver skips events with
   `blocks_availability` false.
5. **Setup:** a `/setup` screen for the Mac path and the scope file.

## First tests on a Mac

In order; each later step assumes the earlier ones.

1. `build.sh` compiles with the Command Line Tools on macOS 14 and 15;
   `otool -s __TEXT __info_plist` shows the plist.
2. `tacitus-native version`; `echo '{...status...}' | tacitus-native call`
   from Terminal (expect Terminal-attributed answers).
3. The agent loads in `gui/<uid>`; `request-access` shows three prompts
   naming tacitus-native; Privacy & Security lists it under Calendars,
   Contacts and Automation > Mail.
4. A rebuild of unchanged source: are the grants kept? A changed build?
5. `list-calendars` shows the Exchange, Google and iCloud calendars, and
   the ids survive a restart.
6. `list-events` for a week: recurring occurrences, all-day end dates,
   availability per source.
7. `create-event` on each source; which support `tentative`.
8. `mail-status` and `list-mailboxes`: the real mailbox names per account
   kind (`ROLE_DEFAULTS`).
9. `list-messages` on a large inbox: time for the bulk id and date reads.
10. Mail's `message id` property: with or without angle brackets;
    `messages.byId()` works.
11. `search-messages`: nested `_and` / `_or` in `whose`; the cost of
    `content contains`.
12. `find-thread`: the cost of `allHeaders contains` over 90 days.
13. `save-draft`: `save()` or `close` with saving; which Drafts mailbox;
    the Message-ID is readable; whether Mail adds a signature.
14. `Mail.reply` with `openingWindow: false` returns an outgoing message,
    its content can be set, its recipients replaced, and its threading
    headers survive.
15. `send-message` sends from the right account, leaves a copy in Sent,
    and threads at a Gmail and an Outlook recipient.
16. `delete-draft` removes the server's copy too.
17. `move-message` to Archive; Gmail's All Mail behaviour.
18. As ea-bot: `osascript` to the owner's Mail and EventKit for the
    owner's calendars both fail.
19. From the mediator's LaunchDaemon: the same calls fail (the reason for
    the agent).
20. Calendar.app scripting: an added attendee gets an invitation on
    Exchange and on Google, or not.
21. EventKit: moving or deleting a meeting the owner organizes notifies
    the attendees, or not.
22. Mail not running: the errors; whether the agent should open Mail.
23. Sleep, wake, log out, FileVault unlock.

## Code map

| File | What |
|---|---|
| `preprocessor/native_accounts.py` | the protocol, the transports, `NativeHelper` and its gated-verb rule, the handles, `NativeScope` and `backends()`, `NativeMailClient`, `NativeCalendarClient`, `DraftRegistry`, `FakeNativeHelper`, the doctor rows, the owner CLI |
| `preprocessor/availability.py` | the providers `graph`, `owa`, `none`, `LocalFirst`, the seam `scheduling_engine._schedule_detail` calls |
| `preprocessor/ea_config.py` | `mail.backend`, `calendar.backend`, `availability.provider` and `get_backends()` |
| `scripts/ea-doctor.py` | `check_native_accounts`; `check_auth` needs no Graph token on a fully native install |
| `deploy/macos/tacitus-native/main.swift` | the helper: EventKit, Contacts, the permission checks, `call` and `serve` |
| `deploy/macos/tacitus-native/mail.js` | the Mail verbs, JXA |
| `deploy/macos/tacitus-native/Info.plist` | the usage descriptions linked into the binary |
| `deploy/macos/tacitus-native/build.sh` | `swiftc`, the plist section, ad hoc signing, `--install` |
| `deploy/macos/tacitus-native/org.ea-system.tacitus-native.plist.in` | the owner's launch agent |
| `deploy/macos/tacitus-native/native-accounts.example.json` | the scope file to copy |

## Sources

- [S1] Accounts framework (deprecated): https://developer.apple.com/documentation/accounts
- [S2] Add your email and other accounts on Mac: https://support.apple.com/guide/mac-help/add-your-email-and-other-accounts-mh35565/mac
- [S3] `requestFullAccessToEvents(completion:)`: https://developer.apple.com/documentation/eventkit/ekeventstore/requestfullaccesstoevents(completion:)
- [S4] `NSCalendarsFullAccessUsageDescription`: https://developer.apple.com/documentation/bundleresources/information-property-list/nscalendarsfullaccessusagedescription
- [S5] `EKSourceType`: https://developer.apple.com/documentation/eventkit/eksourcetype
- [S6] `EKCalendarItem.attendees`: https://developer.apple.com/documentation/eventkit/ekcalendaritem/attendees
- [S7] `EKParticipant`: https://developer.apple.com/documentation/eventkit/ekparticipant
- [S8] `EKEventStore`: https://developer.apple.com/documentation/eventkit/ekeventstore
- [S9] Calendar Scripting Guide, "Add an Attendee to an Event": https://developer.apple.com/library/archive/documentation/AppleApplications/Conceptual/CalendarScriptingGuide/Calendar-AddanAttendeetoanEvent.html
- [S10] `predicateForEvents(withStart:end:calendars:)`: https://developer.apple.com/documentation/eventkit/ekeventstore/predicateforevents(withstart:end:calendars:)
- [S11] `EKEvent.eventIdentifier`: https://developer.apple.com/documentation/eventkit/ekevent/eventidentifier
- [S12] `calendarItemExternalIdentifier`: https://developer.apple.com/documentation/eventkit/ekcalendaritem/calendaritemexternalidentifier
- [S13] `NSAppleEventsUsageDescription`: https://developer.apple.com/documentation/bundleresources/information-property-list/nsappleeventsusagedescription
- [S14] JavaScript for Automation, OS X 10.10 release notes: https://developer.apple.com/library/archive/releasenotes/InterapplicationCommunication/RN-JavaScriptForAutomation/Articles/OSX10-10.html
- [S15] `AEDeterminePermissionToAutomateTarget` (header documentation): https://docs.rs/objc2-core-services/0.3.2/objc2_core_services/fn.AEDeterminePermissionToAutomateTarget.html ; https://developer.apple.com/forums/thread/130949
- [S16] Mojave privacy protection and Full Disk Access: https://eclecticlight.co/2018/09/17/managing-mojaves-privacy-protection-privacy-controls/
- [S17] `CNContactStore.requestAccess(for:completionHandler:)`: https://developer.apple.com/documentation/contacts/cncontactstore/requestaccess(for:completionhandler:)
- [S18] `NSContactsUsageDescription`: https://developer.apple.com/documentation/bundleresources/information-property-list/nscontactsusagedescription
- [S19] `CNContact.predicateForContacts(matchingEmailAddress:)`: https://developer.apple.com/documentation/contacts/cncontact/predicateforcontacts(matchingemailaddress:)
- [S20] TN2083, Daemons and Agents: https://developer.apple.com/library/archive/technotes/tn2083/_index.html
- [S21] Swift Forums, an Info.plist in a command-line tool (`-sectcreate __TEXT __info_plist`): https://forums.swift.org/t/swift-package-manager-use-of-info-plist-use-for-apps/6532
- [S22] A CLI, TCC and the embedded Info.plist (responsible process, `otool` check): https://steipete.me/posts/2025/applescript-cli-macos-complete-guide
- [S23] Microsoft Graph `calendar: getSchedule`, permissions: https://learn.microsoft.com/en-us/graph/api/calendar-getschedule
- [S24] Public and confidential client apps (MSAL): https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications
- [S25] Convert a single-tenant app to multitenant, user and admin consent: https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant
- [S26] Google, OAuth 2.0 for iOS and desktop apps (public clients, PKCE, loopback): https://developers.google.com/identity/protocols/oauth2/native-app
- [S27] Google Cloud, verification requirements (sensitive and restricted scopes, security assessment): https://support.google.com/cloud/answer/13464321
