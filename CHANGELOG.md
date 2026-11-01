# Changelog

Versions restart at the public release: `v0.1.0` is the first public
commit. The sections from `v1.2.0` down are the history of the private
repository that preceded it.

## Unreleased

## v0.2.0 (2026-11-01)

- **Tacitus has its own chat.** A `WebFrontend` serves a web app from the
  bot (`docs/webchat.md`): channels, threads and the DM, cards with their
  buttons, uploads, mentions, history copied from Zulip, and Web Push. It
  runs in shadow mode beside Zulip (`chat.shadow`) or as the only
  frontend; a web-only install has scheduled posts, alerts, the doctor,
  setup and the seed. Setup runs in the browser from a loopback or tailnet
  page with one set of question definitions and the doctor at the end.
- **A phone that leaves the owner's network keeps working.** The app
  installs to the home screen; its files are signed by the bot (an Ed25519
  manifest the service worker checks, re-signed when they change on
  disk). Off the tailnet it talks through a relay on a public host
  (`relay/`, `docs/webchat-relay.md`): end-to-end keys made on the phone,
  pairing from one button in the app or from a link, device approval from
  the owner's DM, and the switch to the relay without a restart when the
  network drops. The phone keeps its key as PKCS#8 bytes, since WebKit
  returns no X25519 `CryptoKey` from IndexedDB. The notes the app shows
  when Tacitus is unreachable say what to do in plain words, and the app
  posts what it saw to the bot's log (`/api/diag`).
- **The look.** Threads in a channel are separate cards; messages sit in
  bubbles, yours on the right; blurred headers, a rounded composer, a
  radius and shadow scale, a raised dark theme, thin scrollbars on a
  desktop. A dual-screen Android device gets one pane per screen with the
  split at the hinge (`horizontal-viewport-segments`), and a two-pane
  fallback at 1000 to 1200 px.
- **A macOS native-accounts backend**, prototyped behind an availability
  seam: EventKit for the calendar and Mail.app through JXA, in place of
  app registrations, with the mediator in front.
- **Registration-free sign-in.** A published Entra app and a Google desktop
  client are wired behind empty ids; the terminal Google sign-in calls the
  client's own setup function.
- **The Docker web chat says what it exposes, and narrows it.** The
  Caddy example strips `Tailscale-User-Login`, `X-Tacitus-Relay` and
  `X-Tacitus-Relay-Cards` from every request, publishes on
  `WEBCHAT_TLS_BIND`, and carries commented `remote_ip` and client
  certificate blocks; `docs/docker.md` states that as shipped it puts the
  sign-in page on the internet, and shows nginx's `limit_req` and
  `limit_conn`. The web chat caps the connections one address other than
  loopback may hold (8 of 64; `peer_connections` in the web block, 0
  behind a proxy, which the Docker setup writes). The entrypoint's
  private-volume step never follows a link (`find`, `chown -h`).
- **The sign-in listener waits for its own redirect.** The loopback
  listener of the Graph sign-in took the first request it got, from any
  local process, and one idle connection held it past its timeout. It
  now answers only a GET to the redirect's path with the flow's `state`
  (anything else gets a 400, noted once), reads connections side by side
  with five seconds each, and binds `::1` beside `127.0.0.1`. A bad port
  in `GRAPH_REDIRECT_URI` is a clear error that falls back to the device
  code.
- **The keep floor covers the admissions rule and the digests.** The
  routing table's keep floor counted only listed domains and addresses;
  it now counts the admissions rule (on unless set false) too, and holds
  `digests` off `hosted` like `mail` and `tagging`. The routing change
  log records the credential of a web save (`cookie` or `header`) and the
  device of a relayed one.
- **Only update.json turns updates off.** `update.channel` in
  ea-config.json, which the EA user can write, now reads as `auto`; the
  doctor warns when it names `latest`, `stable` or `off`, and the owner
  moves it to `~/.ea-private/update.json`. The root step reads the same
  rule.
- **The published app's trust statement is corrected.** An owner of the
  Entra registration can add a redirect URI and a secret without a new
  consent, so the docs no longer say the project cannot widen its own
  access; they name the guards (a dedicated tenant, two owners with FIDO2
  keys, audit alerts on credential and redirect changes, publisher
  verification). The admin-consent link asks for the `email` profile's
  scopes unless `--profile` names another, and `it-request.md`
  recommends "Assignment required". For the published app an owner
  process takes `GRAPH_SCOPES`, `GRAPH_REDIRECT_URI` and
  `GRAPH_AUTH_METHOD` only from `~/.ea-private/secrets.env`, where setup
  now writes the profile's scopes.
- **Updates trust only signed releases on main, against a pinned key.**
  An install pins the release key in the owner-private `update.json`
  (`release_key`), written once by the root step from the first release,
  and verifies every release against it, never against a file in a
  release; a release that changes the key waits for the owner. The
  updater fetches `main` and installs a tag only when its commit is in
  `main`'s history, accepts only ED25519 signatures, and `check` warns
  when no release qualifies. The relay pins its key in its state
  directory. The CI `release` job runs in the `release` environment, pins
  its action by commit, and passes the signing key to one shell step and
  no Python step. Owner steps come only from commit messages and the
  signed tag, never from a Release or pull request body
  (docs/updates.md, Supply chain).
- **One routing table, task by task.** The owner says which model does
  each task (`mail`, `tagging`, `drafting`, `digests`, `briefing`,
  `audit`, `scheduling`, `chat`, `review`): `local`, `hosted` or `auto`,
  where `auto` keeps the remote-LLM switches and is every task's default,
  so nothing changes until the owner sets a value. Each lane asks the
  table where it reads its switch. Two floors sit under it: `review` (the
  still-open check, the nightly reconcile, the scheduling backstop) is
  hosted only, and `mail` and `tagging` cannot be hosted while the keep
  list has entries. The table is the owner-private `routing.json`, set
  from a new Settings screen in the web chat (`/api/settings/routing`) or
  `model_routing.py set`; `routing` in chat prints it, and the doctor's
  `model routing` row names each task's model and the local model's
  state.
- **Releases say what they changed.** The CI `release` job creates a
  GitHub Release for each signed tag, its body the notes
  `scripts/release_notes.py` writes: the pull request titles merged since
  the previous release, each linked, and the owner steps named by an
  `Owner-steps:` line in a pull request body or commit message
  (CONTRIBUTING.md). The updater records the installed release's notes in
  its status file, from the GitHub Release or made again from its cache
  when the API does not answer. The morning briefing line counts the
  changes and names the first two (`Updated to v0.1.7 (3 changes: ...)`)
  with any owner step, `version` lists the first three and the link, and
  the doctor's `updates` row says how many changes the install is behind.
- **A version bump reads only the released commit.** `update.py
  next-version` read `#minor` and `#major` in every commit message since
  the last release, so a message that described the markers raised the
  next release to a new major version. It now reads the released merge or
  squash commit and, in CI, the body of its pull request, and a marker
  counts only as a word of its own or a `release:` footer line.

- **Installs update themselves.** Every push to `main` that passes the
  full CI tiers becomes a release: the `release` job signs the next
  `vX.Y.Z` tag with an SSH key held as a repository secret, and
  `.release-key.pub` is the only trust root. `scripts/update.py`, run
  hourly by `tacitus-update.timer` (or a LaunchDaemon on macOS), installs
  the newest signed release on the install's channel (`update.channel`:
  `auto`, `latest`, `stable`, `off`): it verifies the tag, stages the
  tree under `/opt/ea-system.releases/`, runs a smoke, waits until no
  chat turn or cron lane runs, moves ea-bot's copy, switches a link,
  restarts the services and gives them 60 seconds to come up connected.
  A release that fails is switched back and held back. The doctor gains
  an `updates` row, the morning briefing one line when the version
  changed, and the chat bot a `version` command. `scripts/cut-tag.sh
  --stable` moves the `stable` channel. An install whose tree the
  post-commit hook writes and that has no `update` key is left to the
  hook (docs/updates.md).

- **`stop` and `cancel` work in rooms and the DM.** The cancel check
  read the header the bot puts before a room or DM message, so every
  message there was longer than six words and never a cancel. A `cancel
  that` in a project room waited behind the turn it was meant to stop,
  then ran as a turn of its own and retired the drafting rule the turn
  had just recorded. The check now reads only the user's words. A
  message deleted while it waits behind a running turn no longer starts.
- **Spreadsheets in the chat lane.** `doc_text.py` reads an .xlsx: every
  sheet prints as a table with row numbers and column letters.
  `doc_write.py xlsx --in <rows.csv> --out <path>.xlsx` writes one, and
  with `--template` it fills a copy of a form: the header, styles, widths
  and other sheets are kept, `--start-row N --replace` writes the rows
  from row N and clears the old ones, `--set C4=...` fills one cell. A
  value starting with `=` stays text. A chat turn had to stop because it
  could not open the form a department sent or write the updated one.
  `attach-file` now takes the attachment's type from the file name; it
  sent every file as application/pdf. `openpyxl` joins requirements.txt.
- **`validate-draft` says which times may be proposed.** It answers
  `valid: true` for a time outside working hours or on a p0 day, because
  a sender may offer such a time. A scheduling turn used that to fill an
  empty day with a Friday 5-7 PM time no one asked for. The answer now
  carries `proposable` and `not_proposable` (the times that break a rule
  propose-meeting enforces: wh, nm, p0), and the scheduling prompts say
  such a time is named only when the sender offered it or the user did.
- **A forward is not an answer.** `create-reply` skipped every thread
  whose newest message was the user's own, so a request the user had
  forwarded to a colleague could not get a reply draft. The guard now
  skips only when the user's latest message went to the sender being
  answered; it still fails closed when either message cannot be read.
  The skip names `--force` for a reply the user asked for.
- **A stale reply target in a branched thread is refused.** When a reply's
  message id had gone stale, `create_draft` re-resolved to the newest
  message in the conversation. In a thread the user had forwarded to a
  colleague, that is the aside: the reply went to the colleague and quoted
  it. The re-resolution now applies only to a thread with at most two
  senders; otherwise `create-reply` refuses and names the senders.
- **A booking link's empty days are not free.** The solver treats an
  internal attendee's stated windows as partial, so a colleague on the
  user's own domain who sent a booking link was offered times on days the
  link left empty. `booking_link.py` now marks the windows
  `"availability_complete": true`, and the solver blocks every day they
  leave out; getSchedule still applies on top.
- **The scheduler footer counts every option.** A chat turn keeps 300
  characters of each tool result, and the solver prints its calendar reads
  before its JSON, so the `:calendar:` footer said "1 options" for an
  answer with five. The capture now summarizes the whole result and the
  footer uses that. A `--help` call is no longer traced as a solver run,
  and `booking_link.py propose` and `read` are traced.
- **`booking_link.py` reads Calendly booking pages.** A
  `calendly.com/<user>/<event>` link now feeds the solver as a cal.com
  link does: the event lookup and the open spots come from the public
  GETs the booking page itself makes (`calendly.com/api/booking`), read in
  28-day pieces because Calendly refuses a range longer than about five
  weeks. Nothing is booked; a `calendly.com/d/` link stays with
  `poll_filler.py`. Before, the EA said it could not read the page and
  asked the user to book on the link by hand.
- **The deck tool is a plugin.** `preprocessor/deck_tools.py`, the
  deck-builder skill, its chat trigger and its allowlist line leave the
  core; every owner builds slides their own way, so a Slidev workflow is
  one owner's plugin (`ea-plugin-slidev-decks`), not a capability every
  install carries. The core keeps the hosting boundary: the mediated
  `deck_serve.py` route verb, the `scripts/deck-bin` shims, the resident
  deck ports, and `deploy/enable-deck-builder.sh` for the mounts, node,
  linger and the mediator restart. `deck.theme` leaves the config
  template. A plugin's tool reaches the core through `EA_CORE_DIR`.
- **`email_to_pdf.py --gmail <account>`** prints a Gmail message to PDF,
  as it does an Outlook message: the HTML part from the mediated
  `gmail_client.py get-message`, rendered offline with every request
  refused. A receipt that came only to a Gmail account had no PDF path,
  and the EA asked the user to forward it to Outlook. The page header now
  escapes its From, Subject and Date (a Gmail `Name <address>` sender
  lost its address), a plain-text body is escaped, and the header goes
  inside an upper-case `<BODY>` tag.
- **Plugins can ask for mail as it arrives.** A v2 manifest's
  `capabilities.mail_triggers` names the mail a plugin wants: a sender or
  domain, the tagger's topic tag, a subject substring, bulk mail only. The
  mail tagger's tick copies each match, with its body, to the plugin
  (`plugin_mail.py`), and `ea_plugins.mail.delivered()` reads the copies.
  A plugin that searched for its mail at its own hour missed mail deleted
  in between, since a move changes the Graph id and a mailbox search
  skips Deleted Items. A topic trigger must be bulk-only; kept mail never
  reaches an agent-zone plugin; sealed mail arrives without its body.
- **Drafting A/B pairs a draft with the call that wrote it.**
  `draft_ab.assign` took any `email-author` call in the run as the writer
  of every new Outlook draft: a draft the remote writer made, in a run
  that also called the subagent for a different email, had that other
  email's body put in it before the Send card. The pairing now requires
  the subagent's returned text in the draft (`author_match`), and the
  bot keeps that text whole in the tool log.
- **`invite_eval.py`**: nightly, every invite the scheduling pipeline
  staged is scored against the thread (the accepted time, phone or
  video), the intent (duration, the sender on the event), the user's zone
  and the calendar (created, and at the staged time). One line to the
  health channel, the scores in the owner's store.
- **The thread's own times decide an acceptance.** `sched_thread_state.py`
  reads the clock times in a thread with no model: the user's last message
  naming times is the offer, and the next inbound that names one of them
  is an acceptance, other times a counter. The scheduling detector runs
  it as a third pass over the Sent mail (bodies) and the inbox, so an
  accepted offer reaches the lane even when the remote record is missing
  or wrong, and the record pass's time is checked against the offer.
- **`sched_backtest.py`**: for the scheduling mail of the last N days
  (the remote records' request, proposes-times, reschedule and
  confirmation kinds), what reached the lane: a ledger row, a scheduling
  thread, a card, and whether the user answered by hand. Owner-side; the
  first run is the baseline for the scheduling work. The scheduling
  detector's record pass skips Outlook's own "Accepted:" and "Tentative:"
  replies, which the model labels as confirmations.
- **An accepted time becomes a scheduling turn.** The remote record's
  scheduling kind now feeds the scheduling detector beside the keyword
  list, also in a thread the user has written to: when the other side
  accepts a time the user offered (kind `confirmation`), the proactive
  queue opens a scheduling thread that checks the calendar, stages the
  Create card and drafts the confirming reply. Times proposed back and
  requests the keyword list misses come the same way.
- **A closed thread's row reopens on a newer ask.** A needs-response row
  is keyed by its conversation; a second ask in a thread whose row was
  closed used to be dropped as a duplicate. A message newer than the
  close reopens the row on it, with closure's reopen record.
- **A built deck or poster attaches from where it was built.** The mail
  mediator's attach and download roots gain the deck workspace
  (`EA_DECKS_ROOT`, else the resident's `presentations/`) and
  `~/.ea/exports`; a poster PDF no longer needs a copy the lane cannot
  make.
- **The Send-all card is filed with its drafts.** A "N drafts are
  ready" card whose drafts were all sent, cancelled or deleted on their
  own cards or in Outlook got a status edit and stayed unread in the
  Zulip Inbox; it now moves to the done stream like the draft cards.
- **Stop a running turn.** `stop` or `cancel` (also `never mind`, `wrong
  channel`) in a thread whose turn is still running ends that turn's
  Claude process instead of queueing behind it. The stopped turn posts
  no reply; a draft it made before the stop keeps its Send card, so it
  can be cancelled there. With nothing running, the word is an ordinary
  message. The resident pane and a parked (deferred) message are not
  affected.
- **A Word file from markdown.** `preprocessor/doc_write.py docx --in
  <file.md> --out <path>.docx` writes a .docx (headings, lists, tables,
  bold and italic, page breaks) under `~/.ea/docs`, `letters`, `state` or
  `research`; `--template` keeps a letterhead's styles, headers and
  footers. The chat and cron lanes allow it, so the EA no longer falls
  back to hand-written RTF when a document is wanted in Word.
- **A smaller root directory.** `SETUP.md`, `INSTALL.md` and the
  LLM-harness essay (now `llm-harness.md`) live in `docs/`; the
  scheduling channel's operating files live in `prompts/`; `setup.sh`,
  the two service-account installers and `user_init.py` live in
  `scripts/install/` (`install.sh` stays at the root and calls them).
  The `ea-slack-bot.py` shim is gone: a unit whose ExecStart still
  names it needs `deploy/rename-chat-bot.sh` first. `run-agent.sh`,
  `run-plugin.sh` and `ea-chat-bot.py` stay at the root, so no crontab
  or unit changes.
- **Sends stop filling the Zulip Inbox.** A draft outcome (Send, Sent
  myself, Handled, a send or a delete found in Outlook) names the ledger
  rows it closed on its one status line, instead of one "Closed
  obligation" post per row in the thread. A card settled outside chat (a
  draft sent or deleted in Outlook) is filed with `ChatFrontend.file_card`:
  on Zulip it moves, with its topic and without a notice, to a private
  stream the bot makes on first use (`<dm_stream>-done`, or
  `dm_done_stream`), only while that stream's subscribers are the bot and
  the owner. An edit had left a card the owner never opened unread.
- **A thumbs up never means anything but the card's approval.** A card
  that withholds its approval (a draft or an invite longer than one card
  shows, an unstored event, a plugin card with a cut value) used to give
  the thumbs up, the check and a bare code to the first action left:
  Edit on a draft card, Cancel on an event card. Such a card now has no
  default action (`ConfirmationCard.no_default`, kept on its registry
  row): no thumbs-up chip, and a thumbs up or a bare code selects
  nothing. Edit keeps its pencil chip.
- **The EA can save an image from the web.** `fetch_url.py --url <u>
  --save-image <path>` fetches under the same address policy and log as
  a page read, keeps only a PNG, JPEG, GIF or WebP by its first bytes
  (never SVG), at most 15 MB, and writes a new file (never over one,
  never through a symlink) in a deck's folder or `~/.ea/state`, with the
  suffix of its real type. A page read now also lists the page's images
  (`og:image` first) so the image URL can be found from the page that
  shows it. `fetch_url.py` was already granted in every lane, so no
  grant changes. docs/ea/web-reading.md, the deck skill and
  docs/SECURITY.md describe it.
- **The EA reads Word files again.** Since every headless lane runs with
  `--permission-mode dontAsk`, the improvised `unzip -p` or `python3 -c`
  that used to read a .docx is refused, and no granted tool read one.
  `preprocessor/doc_text.py <path>` prints the text of a .docx (headings,
  lists and tables in order; tracked changes accepted, or marked with
  `--tracked`; reviewer comments anchored and listed with their
  authors), a PDF or a text file. It reads only regular files that
  resolve under `~/.ea/{state,docs,uploads,letters,research}`, with
  size caps, and pages long text. The chat lanes and run-agent.sh grant
  it; CLAUDE.md rule 5d names it.
- **`/setup` goes on without an app registration.** With no registration
  the Outlook step used to stop the install until IT answered. It now
  also offers the owner plugin
  [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth),
  after saying what it does and asking whether the user's organization
  allows it. The steps (Playwright, the install with consent, the first
  sign-in and check, the harvest's crontab line) are SETUP.md "Option C:
  no registration (outlook-web-auth)"; `user_init.py` names the plugin
  where it said "an owner-side plugin".
- **An upgrade path for the Outlook web token path** (#16). The owner
  plugin that v0.1.0 moved this path to is published:
  [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth). INSTALL.md ("Upgrade") and
  docs/AUTHENTICATION.md say how an install on `GRAPH_AUTH_METHOD=playwright`,
  a pasted token or a `token_refresh.py` cron line moves to it. The
  doctor fails `auth: outlook web token path` while such an install has
  no owner plugin supplying a Graph token, or still runs the removed
  `token_refresh.py` from cron, and warns on a `GRAPH_AUTH_METHOD` value
  the core does not read.
- **Briefings also post to project rooms.** After the open-claims audit,
  each item in the morning briefing or the afternoon update that belongs
  to a project with a room is posted in that room too, one message per
  room in a new topic, with up to five other open rows of the project.
  The project comes from the ledger link, then the filed thread, then
  the strict project matcher. The full post still goes to the EA stream
  and ends with the rooms that got a slice. A room post goes only to a
  room the owner's record names (`room:<channel id>` keys in
  `chat_notify.post`, posted through the configured chat frontend), and
  quiet hours hold it. `"briefing_rooms": false` in remote-llm.json
  turns it off.
- **Intake for a public repository.** Issue forms that require the
  version, platform, profile and the doctor's output; `ea-doctor.py
  --share` masks addresses, account names, hosts and ids for pasting;
  Discussions for questions and ideas; SECURITY.md with private
  reporting; SUPPORT.md; Dependabot monthly; a stale rule for
  `needs-info` only.
- **CI.** Pull requests run the lint; the full tiers run on pushes to
  `main`, with the `full-ci` label or by hand. The release suite runs by
  hand only.
- **Scheduling.** The pipeline's invite path says when the meeting is
  already on the calendar and stages nothing; a reply the on-prem model
  wrote names the local tier in its footer.
- **Python 3.10 floor.** Five f-strings no longer nest their own quote
  type; a wall test compiles every file with python3.10 when present.

## v0.1.0 (2026-10-20)

- **Public release as Tacitus**, under AGPL-3.0-only: the README as a
  front page, INSTALL.md, the developer guide in `.claude/CLAUDE.md`
  (kept out of the EA's tree), the EA's portrait on Zulip and Slack.

- **The briefing checks that what it raises is still open.** The remote
  briefing's pre-send audit now asks about every ledger row the post
  shows, by id, not only rows whose words matched and had evidence. The
  evidence adds mail in the row's own thread, archived mail from the
  person a waiting row waits on, and rows closed since with the same
  thread or correspondent, with their notes. Kept mail is removed, and a
  tool-less Claude call gives the verdicts (`briefing_audit` in
  remote-llm.json; the remote model answers when Claude fails). A
  deadline email the user has answered in its own thread is no longer
  overdue work in `today_top`. A do-list item is named once: not again in
  the date banner, "Needs you" or "Sent to Claude".
- **Rooms are the bot's own.** The bot serves, posts in, reopens and
  archives a registered room only when the owner-private
  `~/.ea-private/owner-rooms.json` names it as a room the bot created for
  the owner; a row in `ea-channels.json` alone no longer reopens a stream
  or gets the project pack posted there. The first start takes over each
  registered room whose members are exactly the owner and the bot. On
  Zulip the bot does not start while the EA, scheduling or DM stream has
  a subscriber besides the owner and the bot, and a topic move by anyone
  else re-keys nothing. `owner_trust.py seed` now has every stream the bot
  posts to typed back (or passed: `--chat-stream`, `--chat-sched-stream`,
  `--chat-dm-stream`, `--chat-notify KEY=STREAM`), and the profile name
  too (`--profile-name`).
- **The owner-run evals read only repository content.** A full-mode eval
  (nightly, and the release tests' briefing lane) runs `claude -p` in a
  scratch directory outside any checkout, with config from
  `tests/evals/config/` and the templates, no settings file, no MCP
  server, only Bash for `ledger.py` and Read, the shared deny list, and
  `--permission-mode dontAsk`. The Codex harness is refused where the
  owner can read the resident's home. `eval_full_enabled` and
  `release_tests_enabled` moved from `ea-config.json` to the owner-private
  `~/.ea-private/evals.json`, and the nightly last-commit state to
  `~/.ea-private/evals/last-full-commit`.
- **Publishing a site or pushing to Overleaf is a button.** The EA
  commits in its clone and stages `pending-publish.json`; the bot posts a
  :lock: card with the files the commit changes and a scan of the added
  lines for email headers, email addresses, phone numbers and message
  ids. The click mints a one-time token bound to that commit and the
  remote head the card showed, and `site_tools.py publish` or
  `overleaf_tools.py push` pushes exactly that commit. Both verbs and raw
  `git push` are on the deny list. `site_tools.py commit` and `preview`
  and `overleaf_tools.py preview` are new; `revert` commits the undo
  without pushing. `doc_review.py` is unchanged.
- **The cron lanes have no WebFetch or WebSearch.** `run-agent.sh` and
  `run-plugin.sh` deny both; a scheduled run reads pages with
  `fetch_url.py`.
- **The outbound review cannot be steered by a forged time.** Each of
  its logs keeps its own byte-offset cursor (with the inode and a hash of
  the bytes before it), so a far-future time in `fetch-url.log` no longer
  hides the later records of any log. A future, non-ISO or out-of-order
  time, a line that is not JSON, and a log that shrank, changed or was
  replaced are named in the daily post. New flags: long or encoded path
  segments, query values and DNS labels, many subdomains under one
  domain, a host's count well above its recent days, and a busy new host;
  the hosts first seen that day are listed on one line.
- **More outbound requests reach the review.** `booking_link.py`,
  `list_digest.py`'s redirect walk and `poll_filler.py` log each request
  (`preprocessor/outbound_log.py`); each remote-writer brief is recorded
  owner-side (size, hash, first 200 characters) and counted in the post.
  `fill-poll` uses the configured display name and refuses any other
  `--name`; `owner_trust.py seed --profile` keeps an owner-private copy
  of the name the review checks against. `email_to_pdf.py` renders
  offline with scripts off and aborts every remote request.
- **The outbound review covers the model's web tools.** The bot records
  every WebFetch URL and WebSearch query of the chat and room lanes in the
  owner-private `web-tools.jsonl`; the daily review flags an email
  address, a long query string or an encoded payload there, and any web
  tool call from a cron lane.
- **A send approval is bound to what the draft says.** A Send or
  Schedule card stores a content fingerprint (recipients, subject, body,
  attachments; `preprocessor/draft_fingerprint.py`) computed owner-side
  from the read that renders it. A click on a draft that changed since
  its card sends nothing and posts a new card; the confirm token, the
  send verbs and the scheduled-send signature (payload version 2) carry
  the fingerprint and re-check the draft right before the send. A queued
  send whose draft changed is dropped with an alert, and a version 1
  queue entry is dropped too: schedule it again. Cards show Bcc.
- **No owner id, no clicks.** With `EA_SLACK_OWNER_ID` unset the Slack
  front end refuses every button click instead of taking the first
  clicker as the owner.

- **The user's time zone comes from config.** `preprocessor/user_tz.py`
  reads the profile's `timezone`, then scheduling-preferences.json, then
  the system's zone; no module falls back to a hard-coded zone. The
  scheduling-preferences template leaves `timezone` empty.
- **The online-meeting rule names the configured platform.** The install
  overlay, CLAUDE.md, the scheduling prompts, the pre-send check and the
  scheduling agent's rule check follow `institution.video.platform`.
- **The scheduling and slim reply lanes run outside the deployed tree**,
  each in its own directory under the resident's
  `~/.local/state/ea-lanes/`, so Claude Code adds no CLAUDE.md or
  CLAUDE.local.md to prompts that carry all their instructions. A
  session resumes where it began.
- **Setup renders the install overlay as ea-bot through the sudoers
  rule.** The rule `setup-ea-user.sh` writes (and the macOS and Docker
  installs write the same) lets the owner run any command as ea-bot
  without a password, `<you> ALL=(ea-bot) NOPASSWD: ALL`, since every
  lane runs `sudo -u ea-bot env ...` and `owner-io.sh` runs `sh -c` as
  ea-bot; a rule naming only the overlay render would break them. It
  grants nothing as root (SETUP.md "The sudoers rule").
- **License: AGPL-3.0-only.** `LICENSE` carries the GNU Affero General
  Public License, version 3; `AUTHORS` names the authors, who each keep
  the copyright in their own contributions. Contributions come in under
  the contributor license in CONTRIBUTING.md, which lets the maintainer
  also offer the code under other terms. The owner's account and
  personal data are out of the code, docs, config and skills.
- **Teams DMs and the Outlook-web token paths move to owner plugins.**
  The Teams chat-service read, its `get-teams-dms` verb and the headless
  token harvest are the owner-zone plugin `teams-dms`, which declares the
  verb as an alias on `graph_actions` and the harvest as an owner cron
  job; the preprocessor reads the DMs through `plugin_verbs.call_alias`.
  The pasted token and the Outlook-on-the-web browser harvest are the
  owner-zone plugin `outlook-web-auth`, an auth source that
  `graph_client.authenticate` asks after the MSAL cache. The core keeps
  MSAL silent refresh, the confidential client and the public-client
  flows, and routes an Outlook-audience token from a source to the
  Outlook REST API. `token_refresh.py` leaves the core; its token file
  rule is `owner_trust.token_file_mode`, and `owner_trust.py token-modes`
  sets it on the harvested token files (`deploy/harden-permissions.sh`
  step 4). Owner plugin verbs and cron jobs run with `EA_CORE_DIR` and
  the core on `PYTHONPATH`.
- **A parked trial leaves the tree.** The decision-model trial parked on
  2026-10-17 (its client, local server, backtest, live recorder and
  their hooks in the mail tick, the scheduling pipeline and the daily
  ghost post) is removed, with its two secret names. Its history is in
  a private archive repo.
- **The associate-editor workflow lives in its plugin.** The
  reviewer-invite card, its handlers and its poller leave the bot; the
  manuscript-reviewers plugin declares the card (`capabilities.cards`,
  same look, same Send and Skip), and its executor checks the click's
  token with `plugin_cards.consume_token`. The weekly coverage check is
  the plugin's `coverage` schedule (`run-plugin.sh manuscript-reviewers
  -- --coverage`, remote model first) instead of a `run-agent.sh` arm, and
  the ScholarOne tool denials and the ManuscriptCentral mail rules come
  from its manifest. The resident pane and the ea-bot shell now add the
  enabled plugins' tool denials too, as the other lanes do. Slack renders
  a plugin card like the other plain cards.
- **The University of Colorado adapters live in an institution plugin.**
  The CU CS travel form submitter, the CU budget-justification filler and
  skill, and the CU letterhead style move to the cu-boulder plugin. The
  core keeps the generic part: the reimbursement guide builds the trip
  packet and hands it to the user when no plugin files it;
  `compile_letter.py` takes a required asset from the template directory,
  then from `institution.letterhead.asset_dirs`; the calendar noise list
  no longer names a college, which belongs in the profile's
  `boilerplate_tokens`.
- **Scheduling pipeline: the shapes the shadow handed off.** Three days
  of shadow turns sorted into shapes; the free-form agent was 35% wrong
  on them, so each went into the pipeline, where code owns the numbers.
  Offered times are validated first and, when none is free, a
  counter-offer is proposed and said; an empty window is retried without
  CC'd attendees and widened past a single day, each said; "full
  availability" lists up to eight options; "send the invite" with an
  agreed time stages the event for the Create button (a hold for "my
  calendar"); a lowercase name after a preposition is found and a
  request's first word is not taken for one; a follow-up that names no
  one stays on the previous turn's thread. The bot runs the pipeline on
  a pure scheduling ask in any lane the arm is enabled for, rooms
  included. The release suite carries one case per shape with a canned
  thread (`docs/EVAL.md`, `docs/llm-lanes.md` 2.18).

## v1.2.0 (2026-10-16)

- **Release tests** (`tests/release/run_release.py`, `docs/EVAL.md`).
  The LLM-facing suite, run against the models in effect: the briefing
  fixtures on the bot's Claude default, drafting briefs through the
  remote writer (or Claude when the switch is off), scheduling requests
  through the remote scheduling agent with writes recorded. Every case
  is scored; a run is compared to the last one, and the verdict is
  `green`, `amber` (a lane's mean fell), `red` (a regression case
  failed) or `harness` (the models were not reached). Saturday 23:00 on
  cron (`run_weekly.sh`, skipped when code, models and switches are
  unchanged; alerts to the health channel), and on every tag cut
  (`scripts/cut-tag.sh vX.Y.Z`, which refuses to tag on red). Toggle
  `release_tests_enabled`. A bug that involves the model's judgment
  becomes a case in its lane.

### Fixes from the first install of v1.0.1 by someone other than the author

- **The un-gated calendar is per-install.** The id of the EA-Managed
  calendar, the one calendar the EA may write without a button, was a
  constant in `mail_mediation.py`: the author's. On any other install
  every hold and every reconciler write was refused. The id now comes
  from the owner-private `~/.ea-private/calendars.json`, written once
  by `owner_trust.py seed --calendar` from the installer's
  `scheduling-preferences.json`; the mediator reads only that file,
  because the config file is ea-bot's own. With no such file, no
  calendar write is un-gated. `ea-doctor.py` checks that the owner copy
  exists and matches the config.
- **ea-bot gets its own Claude Code.** The setup scripts took the
  owner's `which claude` for the sudoers entry, which on macOS (and on
  any native install) is a path under the owner's home; the owner's
  home is private, so ea-bot could not execute it, and the only way
  out was a job copying the owner's binary across, which no one should
  run. Both scripts now install Claude Code for ea-bot in ea-bot's own
  home with the native installer (self-updating) and name that binary
  in sudoers. `ea-doctor.py` and SETUP.md 8b say the same.

### Upgrade

- Run `python3 preprocessor/owner_trust.py seed --calendar` once as
  yourself. Until then holds and the reconciler's writes are refused
  and the doctor reports the missing owner copy.
- If ea-bot runs your own `claude` binary from a system path, nothing
  changes. If sudoers names a path under your home, give ea-bot its own
  install (SETUP.md 8b, step 0) and point sudoers at it.

## v1.1.0 (2026-10-15)

Ledger closure. No row closes by mistake, and every row that is done is
closed or put in front of you to confirm with one click.

### Closure

- **One closure policy** (`preprocessor/closure.py`). Every closer (the
  scrubber, `ledger_sync`, the mail tick, scheduled sends, the pre-send
  audit, item feedback, the dashboard, the admissions filter and the
  bot's send paths) names its evidence, and one function decides. These
  close a row on their own: your word or click, a standing rule, a
  duplicate, a draft, event or scheduled send that names the row, your
  reply on a row that owes a reply, your archive of a detector row's own
  mail, and the evidence a row declares with `closes_on`.
- **Everything else is a proposal.** The row stays open, and the bot
  posts one card in your DM: Close all, or one by one with Close or
  Still open. The briefings list such rows on a "Looks done?" line and
  keep them out of the queue. Model lanes (the briefing, the audit, the
  mail tick) only propose; a plain `ledger.py close` from a lane records
  a proposal and says so.
- **"Still open" and a reopen hold.** No evidence from before them closes
  or proposes the row again, under any signal name. Newer evidence on
  such a row proposes; only your word, a duplicate or a send that names
  the row closes it.
- **A send closes what its draft names.** The draft verbs take
  `--ledger-id` (Outlook and Gmail). A send (the Send button, Sent
  myself, Handled, or a send from Outlook) closes the rows its draft
  names and the thread's reply row; other rows on the thread are
  proposed, because a reply in a thread can be about something else in
  it.
- **A row declares what finishes it:** `ledger.py add --closes-on
  reply|submission|event|manual`.
- **The EA's own mail moves never read as your archive.** Every move the
  EA makes is recorded (owner-private); `ledger_sync` closes one row per
  archived mail, with no sweep of the thread, and a close archives the
  thread only when nothing on it is still open.
- **A history.** Every close, reopen, proposal, confirm and reject is a
  `ledger_events` row; `ledger.py closure-report --days N` counts
  per signal the closes later reopened, the proposals confirmed and
  rejected, and the closes you had to state.
- New ledger verbs: `propose`, `confirm` (owner only), `reject`,
  `proposals`, `closure-report`. `close` takes `--user-said`,
  `--duplicate-of`, `--user-rule`, `--no-sweep`.
- On the labelled corpus of 107 past cases: no wrong closes, 96% of done
  rows closed or proposed, 68% closed with no click.

### Also

- **A kept email that asks for something now reaches the ledger.** Under
  `mail = enforce`, mail on the keep-from-Anthropic list reached only the
  remote digest, which nothing reads; an AE recommendation nudged three
  times from ScholarOne made no row and missed its date. The mail tick
  writes a row from the remote model's record (subject, one-line
  summary, ask, deadline; no text), typed by the record's state, folds
  repeat nudges onto it, and proposes its close from the thread's records.
- **Mail rows are typed by the record.** A dated todo or decision makes
  a `deadline` row, an undated one a `task` row; archiving the nudge no
  longer closes the work it was about. A dated reminder the model files
  as FYI is repaired to a todo.
- `ledger.add_entry` takes `deadline` and `metadata`.
- **The router's always-on work runs with the resident off.** The
  late-draft sweep, the Outlook-send check and the outbox drain sat
  behind `resident_mode`, so a cron agent's draft got no Send card and a
  card whose draft was sent from Outlook kept its buttons.
- **Tool gateway.** The step budget counts the current turn only and
  does not apply to a request with no tools; call markup never reaches a
  reply.
- **The EA says "chat", not "Slack"**, in rule sources, confirmations,
  room notes and the guides.

### Upgrade

- Add the hourly closure check to your crontab (SETUP.md, "Background
  maintenance"): `closure_scrubber.py check` at 20 past the hours
  between briefings.
- The `ledger_events` table is created on first use. Existing rows keep
  working; a row with no `closes_on` follows its type.

## v1.0.1 (2026-10-14)

Two fixes to v1.0.0, found on the author's install the same evening.

- **The resident could pass the owner test.** v1.0.0 moved the private
  directory's default to `.ea-private` in the calling user's own home.
  A resident process whose home holds such a directory then counted as
  an owner process, so the ledger, the project store and the style
  profiles ran the owner-only database checks as the EA user and
  refused a WAL sidecar the owner's processes had created
  (`UnsafeDatabase`); the EA recorded nothing on such a turn.
  `owner_trust.is_owner_process()` now answers False for the process
  whose own home holds the resolved EA tree, whatever it keeps at its
  own `.ea-private`. The doctor asks the EA user itself ("resident
  trust") and fails when it answers as the owner.
- **The pipeline arm was never drawn.** The remote-LLM config loader
  dropped `ab.arm`, so a window set to `pipeline` drew the on-prem
  harness arm. The loader keeps the key. The pipeline also marks a
  solver option a p1/p2 token penalised in the pick listing and names
  the preference beside the time in the channel line.

## v1.0.0 (2026-10-14)

The first release meant for an install other than the author's. Three
install profiles (`email`, `review-only`, `assistant`), chat on a
self-hosted Zulip server or on Slack, and an optional remote writer.

### What is in it

- **Email triage and the obligations ledger.** Outlook through the
  Microsoft Graph API; every actionable mail becomes a ledger row that
  closes from evidence (replies, calendar responses, archives). Morning
  and afternoon briefings, a weekly audit, intraday intake.
- **Drafts in your voice.** Per-contact style profiles from sent mail;
  bodies written by the `email-author` subagent on Claude, or by the
  **remote writer** on any OpenAI-compatible https endpoint you run
  (`remote_llm.py init ... --drafting remote`; SETUP.md, "Optional:
  Remote writer"). Every draft is reviewed on one card (Send, Edit,
  Handled, Sent myself, Cancel) before anything leaves.
- **Chat on Zulip or Slack.** One interface behind both. Zulip is the
  recommended platform: the compose stack in `deploy/zulip/`, the
  bootstrap and switch scripts, topics as threads, reaction chips as
  buttons. `#ea-<project>` rooms with a persistent specialist session,
  `#tmp-` rooms that mirror a session, a private line for every
  confirmation card.
- **Scheduling.** An OR-tools solver over Outlook and Google calendars,
  rules kept as tokened events on an EA-Managed calendar, reschedule
  detection, poll filling, holds the EA parks and clears itself, and a
  Create, Reschedule or Delete button for every real event.
- **Projects, people and standing rules.** The project store and its
  context packs; preferences recorded in your own words and injected by
  scope into every prompt.
- **Safety model.** The LLM runs as a sandboxed Unix user that holds no
  send-capable credential; mail and calendar writes go through a
  mediator that runs as you; sending, event creation, moves, deletes and
  deferred sends need a one-time token the bot mints only after your
  click.
- **Plugins.** Scheduled plugins on cron with their own channel;
  interactive plugins loaded by keyword.
- **Optional layers.** The remote-LLM lanes beyond the writer (the mail
  gate, digests, briefings, a scheduling agent with a Claude backstop),
  the deck builder, GitHub Pages sites, Overleaf pushes, the doc-review
  pull-request flow, the backup mirror. Each is off until you enable it.

### Changed for this release

- The remote writer's API key is `REMOTE_LLM_API_KEY` in the owner's
  `secrets.env`. `remote_llm.py init --endpoint <https URL> --model
  <name> [--drafting remote]` writes the switch file; `/setup` offers
  the writer as its last optional screen; the doctor reports every
  remote switch in force.
- Owner-specific values moved from the code and the skills into config:
  group lists (`group-senders.json`), deadline sender categories
  (`deadline-senders.json`), the GitHub bot identity, the doc-review
  repo and the published sites (`github.json`), family members
  (`user-profile.json` `family`) and family calendars
  (`scheduling-preferences.json` `family_calendars`). The code ships no
  personal fallback for any of them.
- The owner's private directory defaults to `.ea-private` in the
  owner's own home; the sentinel reads the checkout it runs from and
  `~/crontab.master`; resident state files live under the EA user's
  tree. The Zulip compose file takes the host and the admin address
  from `deploy/zulip/.env` (`ZULIP_EXTERNAL_HOST`, `ZULIP_ADMIN_EMAIL`).
  The mirror requires `MIRROR_DEVICE`.
- The documentation, the skills and the EA's topic guides describe "the
  user" and "the owner"; command examples use `/home/<you>` and
  placeholders.

### Known limits

- The install is written for a CU Boulder account: `colorado.edu` is
  the home domain in several triage heuristics, the reimbursement
  adapter targets the CU CS finance form, and the admissions filter
  reads CU wording. Another institution needs those defaults changed.
- `America/Denver` is the fallback timezone in a few modules when
  `scheduling-preferences.json` has no `timezone`.
- The AE reviewer-coverage check, the budget-justification helper and
  the Teams DM harvest are tuned to one person's roles and tenant.
- CI runs the lints and the template checks; the test wall runs on the
  developer's machine (`python3 tests/run_wall.py`).
