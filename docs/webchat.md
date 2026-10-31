# Tacitus web chat: the contract (v0, 2026-10-30)

Tacitus ships its own chat interface so an install needs no Zulip or
Slack. The bot process serves a small web app (a PWA) over HTTP on
loopback; the owner reaches it over their tailnet (`tailscale serve`
proxies a path on an HTTPS port of the chat's own to the port and adds
identity headers) or, off the tailnet, through a Tacitus relay that sees
only ciphertext (`docs/webchat-relay.md`). In **shadow mode**
the bot keeps its primary front end (Zulip today) and mirrors every
post, update, card and reaction into the web front end, and accepts
input from either side, so the owner can try the new interface against
live traffic. The security rules (who is the owner, the origin rule,
what the bot serves at all) are in docs/SECURITY.md, Web chat; each is
restated below where it applies.

Three parts, built in parallel against this contract:

- **A. `botcore/botcore/chat/web_frontend.py`**: `WebFrontend(ChatFrontend)`,
  the store, the HTTP server and the SSE event stream. Serves the static
  UI from `botcore/botcore/chat/webui/`.
- **B. `botcore/botcore/chat/webui/`**: the static web app (no build step:
  `index.html`, `app.js`, `app.css`, `manifest.webmanifest`, `sw.js`, icons).
- **C. `botcore/botcore/chat/shadow_frontend.py`**: `ShadowFrontend(ChatFrontend)`,
  a composite that drives a primary and mirrors into a shadow; the
  `load_frontend` wiring.

Code rules (from `.claude/CLAUDE.md`): no new third-party dependency for
A or C (stdlib `http.server`, `sqlite3`, `json`, `threading`; SSE, not
websockets); no real names, addresses or mail text in code, tests, commit
messages or PR text; comments in present tense; tests beside the existing
ones in `tests/`; the root `CLAUDE.md` is the EA's operating file and is
not edited here.

## Config

The chat block (`~/.ea-private/chat.json` for owner processes, read by
`preprocessor/ea_config.get_chat`; `ea-config.json` otherwise). The bot
serves the web chat only from the owner-private file
(`ea_config.get_chat_source` tells, and `chat.load_frontend` passes
`trusted_config`); a block from `ea-config.json` builds a front end whose
start() refuses (Identity and auth):

```json
"chat": {
  "frontend": "zulip",
  "shadow": "web",
  "web": {
    "bind": "127.0.0.1",
    "port": 8470,
    "base_path": "/chat",
    "public_url": "https://<host>.ts.net:8444/chat",
    "tailscale_login": "<owner login as tailscale serve reports it>",
    "trust_tailscale_header": true,
    "pair_token_file": "<private_dir>/webchat-pair.token",
    "backfill_days": 7
  }
}
```

`"frontend": "web"` with no `shadow` runs the web front end alone. An
optional `notify` map takes the notifier's channel keys and names a web
channel for each (`{"personal": {"channel": "dm"}}`); a key it does not
name posts to the EA channel (CLI senders). An
optional `push_subject` key (a `mailto:` or `https:` URL) is the contact
the push services see (Notifications, below). An optional `relay` block
turns on the relay (`docs/webchat-relay.md`, Config); it counts only from
the owner-private `chat.json`.
`"shadow": "web"` with any primary wraps the primary in `ShadowFrontend`.
`FRONTENDS` in `preprocessor/chat/__init__.py` gains `"web"`.
`get_chat` defaults every `web` key (port 8470, base_path `/chat`, bind
loopback, trust_tailscale_header true, backfill_days 7). `public_url`
carries the chat's own HTTPS port: one app per origin (Identity and
auth). The store is `<private_dir>/webchat.sqlite`
(owner process) or `<ea_dir>/state/webchat.sqlite` otherwise; a
`store_path` key overrides.
`backfill_days` is the history a shadow copies in when the bot starts
(see Shadow mode); 0 copies none.

## Identity and auth

One owner, one bot. Standalone, `owner_id = "owner"` and
`bot_user_id = "bot"`. A request is the owner when either holds:

1. Header `Tailscale-User-Login` equals `tailscale_login`, on a
   connection tailscaled itself opened. tailscale serve sets the header
   and strips a client-supplied one, but the server binds loopback, and
   every local process (and a page that rebinds a name to 127.0.0.1)
   reaches it. So the header counts only when the client end of the TCP
   connection, found in `/proc/net/tcp` and `/proc/net/tcp6`, belongs
   to tailscaled's uid (`peer_uid`, `tailscaled_uid`). Off Linux, and
   with `"trust_tailscale_header": false`, this path is off. Or:
2. Cookie `ea_web` equals the pair token. `POST /api/pair` with
   `{"token": "..."}` matching `pair_token_file` sets the cookie
   (HttpOnly, SameSite=Strict, Secure when `public_url` is https). A
   token file shorter than 16 characters is refused with a log line.

Anything else: 401 JSON `{"error": "not the owner"}`. Static files are
served without auth; every `/api/` route needs it.

Before auth, every `/api/` request (the origin rule; one app per
origin, so a deck on another port of the host is another origin):

- `Host` is exactly `public_url`'s host and port, or `127.0.0.1`,
  `[::1]` or `localhost` with the server's port; else 421.
- `Sec-Fetch-Site` of `cross-site` or `same-site` is refused (403), a
  GET too.
- A POST carries an `Origin` equal to `public_url`'s scheme, host and
  port; a missing one is refused (403). A loopback client that sends no
  identity header (the relay client) may send the server's loopback
  origin, `http://127.0.0.1:<port>`.

The bot serves only a trusted block (`trusted_config`), a store
directory of mode 0700 and a store and token file of mode 0600 that it
owns, outside the resident's tree (`untrusted_roots`:
`chat.untrusted_roots()`, the EA tree and the resident's home). Else
start() sets `refused` and logs why: standalone it raises ChatError, and
under a ShadowFrontend the composite switches the shadow off
(`shadow_off`) and the bot runs on the primary alone.

## Store (SQLite)

- `channels(id TEXT PK, name TEXT, kind TEXT, archived INT, description TEXT)`;
  kind is `main`, `sched`, `dm` or `room`.
- `messages(seq INTEGER PK AUTOINCREMENT, channel TEXT, id TEXT, thread_key TEXT,
  author TEXT, kind TEXT, text TEXT, card TEXT, file TEXT, reactions TEXT,
  posted_at REAL, updated_at REAL, history INT, UNIQUE(channel, id))`.
  `author` is `bot` or `owner`; `kind` is `message`, `card`, `status`
  (a card replaced by its final line) or `file`; `card`/`file`/`reactions`
  are JSON text or NULL. `history` is 1 on a row copied from the
  primary's past (Shadow mode); such a row never counts as unread.
- `events(seq INTEGER PK AUTOINCREMENT, at REAL, type TEXT, channel TEXT,
  id TEXT, payload TEXT)`: the SSE log; `type` is `message`, `update`,
  `reaction`, `card`, `status`, `file`, `channel`.
- `files(id TEXT PK, channel TEXT, name TEXT, path TEXT, mimetype TEXT, size INT, at REAL)`;
  uploads land under `<store dir>/webchat-files/`.

Standalone ids: messages `w<seq>`, threads `t<seq of the parent>`,
rooms `r<n>`, files `f<n>`. In shadow mode every id is the primary's
(see Shadow mode); the web store never mints one.

## Message JSON (one shape everywhere: REST and SSE)

```json
{"seq": 12, "channel": "C1", "id": "w12", "thread_key": "t12",
 "author": "bot", "kind": "message", "text": "markdown...",
 "card": null, "file": null, "reactions": ["eyes"],
 "posted_at": 1761900000.0, "updated_at": 1761900000.0, "reply_count": 3}
```

`card` is the `ConfirmationCard` as JSON (`dataclasses.asdict`; see
`botcore/botcore/chat/cards.py`: title, fields, body, quoted, actions,
selects and the rest as they are). `file` is `{"id","name","url","mimetype","size"}`.
`reactions` is a list of `Status` values (`types.py`), the strings as
Slack names (`eyes`, `hourglass_flowing_sand`, `white_check_mark`, `x`,
`warning`, `double_vertical_bar`, `question`).

Text is the markdown the core already writes for Zulip (`**bold**`,
`*italic*`, `` `code` ``, fenced code, `- ` lists, `> ` quotes, `#`
headings, `[text](url)`, `:emoji_name:` shortcodes). The UI renders that
subset and escapes everything else; it never injects HTML from text.

## HTTP API (every path under `base_path`)

| Verb | Path | Body / result |
|---|---|---|
| GET | `/api/me` | `{"owner": "...", "bot": "...", "public_url": ..., "push": bool, "relay": bool, "card_actions": bool}` |
| GET | `/api/channels` | `[{"id","name","kind","archived","last_at","unread"}]` |
| GET | `/api/channels/{ch}/threads?limit=50&before=<seq>` | thread parents, newest first, each with `reply_count` and `last_at` |
| GET | `/api/channels/{ch}/threads/{thread_key}` | the thread, oldest first, parent first |
| POST | `/api/channels/{ch}/messages` | `{"text", "thread_key"?}` → the stored owner message; dispatches an `InboundMessage` |
| POST | `/api/channels/{ch}/files` | multipart `file`, optional `text`, `thread_key` → the stored `file` message; dispatches an `InboundMessage` with `files=[FileRef]` |
| GET | `/api/files/{id}` | the bytes, `Content-Disposition: attachment` |
| POST | `/api/cards/{ch}/{id}/actions` | `{"action_id", "value"?, "selected"?}` → `{"ok": true}`; dispatches an `ActionEvent` (`source` `button` or `select`) |
| GET | `/api/events` | SSE; `Last-Event-ID` or `?since=<seq>` replays from that seq; `id:` is the event seq; `event:` is the type; `data:` is the message JSON (`channel` events carry the channel row); a `: ping` comment every 20 s |
| POST | `/api/pair` | `{"token"}` → sets the cookie |
| GET | `/api/push/vapid` | `{"key": "<base64url P-256 point>"}`, the `applicationServerKey`; 404 when push is off |
| POST | `/api/push/subscribe` | the browser's `PushSubscription.toJSON()` → `{"ok": true}` |
| POST | `/api/push/unsubscribe` | `{"endpoint"}` → `{"ok": true, "removed": bool}` |
| GET | `/api/push/subscriptions` | `[{"id", "host", "created_at", "last_seen", "last_ok", "failures"}]`; `id` is a hash of the endpoint, which is never shown |
| POST | `/api/push/revoke` | `{"id"}` → `{"ok": true, "removed": bool}` |
| POST | `/api/relay/pair` | `{}` → `{"link", "qr", "expires", "origin"}`: a one-time pairing link for a phone (the relay), on the bot's own origin; 409 without `public_url` |
| GET | `/api/relay/devices` | `[{"id", "name", "paired_at", "origin", "pending", "code"?}]`, the phones paired with the relay; a new one waits with its pairing code |
| POST | `/api/relay/approve` | `{"device"}` → `{"ok": true}`: let a waiting phone in; 404 for an unknown device |
| POST | `/api/relay/revoke` | `{"device"}` → `{"ok": true}`: its sessions and streams end, its push subscriptions go; 404 for an unknown device |
| GET | `/api/relay/status` | `{"enabled": bool, ...}`: the relay client's state (`connected`, `install_id`, `bot_public`, `client_key`, `devices`, `pending`, ...) |

The five `/api/relay/` routes, and `/api/pair`, answer only on the
tailnet: a request the relay client marks with `X-Tacitus-Relay` gets
403, and the client never forwards them. With no relay running,
`/api/relay/status` says `{"enabled": false}` and the other four answer
404. `card_actions` is false for a request the relay marks as one that may
not confirm a card (`docs/webchat-relay.md`, Where the app's code comes
from).

Channel ids in URLs are URL-encoded (Zulip ids carry characters). A
thread_key is likewise encoded.

Errors: JSON `{"error": "..."}` with 400/401/403/404/421/429. The server
is `http.server.ThreadingHTTPServer` on a daemon thread started by
`start()`, at most 64 connections at once (one more is closed at once)
and a 30-second socket timeout per connection; `run_forever()` blocks on
a stop event; SSE handlers block on a per-connection `queue.Queue` fed
by the store's event fan-out.

## The front end's verbs (A)

`WebFrontend` implements every abstract verb of `ChatFrontend`
(`botcore/botcore/chat/base.py`) over the store, the way
`MemoryFrontend` does (`memory_frontend.py` is the model for shape and
for the dispatch helpers `inject_message`, `click`). `post_card` renders
nothing itself: it stores the card JSON (kind `card`) and the UI renders
it; capabilities `{"buttons", "select", "files", "private_rooms", "reactions"}`;
`card_fits` always true (limits generous: `Limits(20000, 20000, 200, 0)`).
`prompt_strings()` returns web wording (platform "the Tacitus web chat",
format rule markdown, confirm location "the Send button is on the card in
your DM thread"). `llm_tool_grants()` returns "".

Mirror entry points for shadow mode, all idempotent on `(channel, id)`:
`mirror_channel(id, name, kind, archived)`, `mirror_post(ref, text, author)`,
`mirror_update(ref, text)`, `mirror_card(ref, card)`, `mirror_card_status(ref, text)`,
`mirror_reaction(channel, msg_id, status, on: bool)`, `mirror_file(ref, file: FileRef, author)`.
Each writes the row and appends the event. Two more serve the history
copy and moved threads: `mirror_backfilled(channel)` appends one
`channel` event, and `mirror_move(old_channel, old_key, new_channel,
new_key)` re-keys a thread's rows, with an `update` event each.

Inbound dispatch from the HTTP layer goes through `self.registry`
(`dispatch_message`, `dispatch_action`) with `user_id = self.owner_id`.
In shadow mode `ShadowFrontend` sets `web.inbound = <callable>` so the
web front end hands the owner's web input to the composite instead of
dispatching it itself (see below).

## Shadow mode (C)

`ShadowFrontend(primary, shadow)`:

- Attributes (`name`, `capabilities`, `limits`, `main_channel`, ids,
  `card_registry`, `registry`, `thread_namer`) come from the primary;
  `name` is `"<primary>+<shadow>"`.
- `start()` starts both; on start it mirrors the channel list it knows
  (main, sched, DM and the rooms from `archived_map()` that
  `room_filter` keeps; the bot keeps the rooms it made for the owner, so
  no other private stream reaches the web), then starts the history copy.
  `run_forever()` runs the shadow's server thread and the primary's loop.
- History copy (`backfill_days`, from `web.backfill_days`): on a daemon
  thread, for each mirrored channel, `recent_messages` over the window,
  then `thread_replies` per thread, each message mirrored quietly
  (`mirror_post`/`mirror_file` with `quiet=True`) under the primary's id
  and time, author `owner` for the primary's owner id and for a bot post
  whose id the web side recorded as the owner's input (`owner_posted`;
  its `**You (web):**` mark is dropped), else `bot`. A bot post that
  only starts with the mark stays the bot's. A card goes in as the text
  the primary shows. A quiet row has no event and never counts as unread, a
  stored row is left as it is (a second start adds nothing), and a row
  older than its thread's newest is left out, so a thread stays in time
  order. One `mirror_backfilled` event per channel that took rows; at
  most 5,000 messages; a read the primary fails ends that channel's
  copy. Live mirroring waits for the copy (up to 60 s), so the replies
  the bot posts on waking land after the history of their thread; an
  upload and the owner's web input never wait.
- Outbound: `post`, `update`, `react`, `unreact`, `post_card`,
  `update_card`, `replace_card_with_status`, `upload` call the primary,
  then mirror the result into the shadow with the primary's refs. A
  shadow failure is logged and swallowed; the primary's result is
  returned. `create_private_room`, `archive`, `unarchive`,
  `set_description`, `invite` act on the primary and mirror the channel row.
- Reads (`thread_replies`, `recent_messages`, `channel_info`,
  `channel_members`, `archived_map`, `message_posted_at`, `download`,
  `permalink`, `room_url`, `mention`, `is_dm`, `dm_channel`,
  `prompt_strings`, `llm_tool_grants`, `publish_home`, `open_modal`) go
  to the primary.
- Inbound from the primary: the composite wraps the primary's registry
  dispatch so an owner `InboundMessage` is mirrored into the shadow
  (`author = "owner"`) before the core handles it and a `channel_rename`
  renames the row; a `thread_moved` event (a whole Zulip topic move)
  re-keys the thread's rows when its new channel is mirrored; an
  `ActionEvent` is passed through.
- The primary's own writes: the composite sets `primary.on_outbound`
  (an optional `ChatFrontend` attribute, None elsewhere). Zulip calls it
  for the owner's card code (never dispatched as a message), its done
  reaction and the reply to an unknown word; for an edit past the edit
  window (the web row is updated in place); and for a stream's
  description or archive change. The chips on a Zulip card are not
  mirrored: the web card has its buttons.
- Inbound from the shadow (the owner typed or clicked on the web): a
  message is first posted into the primary **as the bot** with the
  prefix `**You (web):** ` so it has a primary id and sits in the
  primary thread, then dispatched to the core as an `InboundMessage`
  with `user_id = primary.owner_id`, that primary id and thread key; the
  web row is stored under the primary id as `author = "owner"`. The web
  side keeps that primary id in `<store dir>/webchat-owner-ids.json`
  (mode 0600, the newest 5,000; `note_owner_post`, `owner_posted`): the
  history copy and `chat_read.py` take a bot post as the owner's only by
  this list, never by the prefix, which anything that posts as the bot
  could write. A click
  is dispatched as an `ActionEvent` whose `card` ref is already the
  primary's. A file goes through `primary.upload` the same way.
- Tests use two `MemoryFrontend`s (or a `MemoryFrontend` primary and a
  real `WebFrontend` on an ephemeral port) and assert both sides hold
  every post, update, card, status and reaction with the same ids.

`load_frontend` returns the composite when the chat block names a
`shadow`; `"frontend": "web"` returns a bare `WebFrontend`. A
command-line sender in an owner process gets the composite too, with a
writer-only web side (see CLI senders).

## CLI senders

Much of what the owner reads is posted from outside the bot process: the
morning briefing, the cron lanes' reports, the room posts, the uploads.
The store and the event streams live in the bot, so a sender writes the
store and the bot pushes what it wrote.

**Who runs what.** The bot (`ea-chat-bot.py`) runs as the owner from the
owner's code tree (`/opt` on a host install, the `bot` service in
Docker); it is an owner process (`owner_trust.is_owner_process`), and
its store is `<private_dir>/webchat.sqlite`, mode 0600 in the 0700
private dir. The cron lanes (`run-agent.sh`, `run-plugin.sh`) run as the
owner too: Claude runs inside them as `ea-bot`, and the wrapper posts its
output as the owner (`slack_notifier.post_to_slack`, then
`chat_notify.post`). Owner plugins, scheduled sends, the alert flush and
`chat/direct.py` are owner processes as well. The resident (`ea-bot`)
holds no chat credential and may not open the store: `chat_upload.py`
run as ea-bot stages its job in the outbox (`slack_outbox`, which serves
every front end), and the bot sends it through its own front end (the
composite, which mirrors it, or the web front end alone).

**The rule.** A command-line sender writes the store when it is an owner
process, so the store it computes is the bot's file and the user is the
bot's user. Any other process posts to the primary alone and logs one
warning per process: the store is the owner's, and the copy it could
open (`<ea_dir>/state/webchat.sqlite`) is one the bot never reads.

**The senders.**

- `chat.load_frontend(for_cli=True)` (chat_upload, chat_read, the room
  posts of chat_notify, `chat/direct.py`) returns the composite with the
  web side writer-only (`WebFrontend(..., writer_only=True)`): `start()`
  opens the store and nothing else (no HTTP server, no event stream, no
  pairing token, no channels made), and load_frontend calls it. The
  mirrors run as in the bot, under the primary's ids. A store that does
  not open is logged and the sender runs the primary alone.
- `chat_notify.post` and `post_as_bot` on Zulip send with a bare Zulip
  client, so they mirror each part themselves through
  `chat.web_writer()`: channel `stream:<id>`, the Zulip message id, the
  thread key `stream:<id>:topic:<topic>` (what the Zulip frontend's
  `post` returns for the same post) and the core's mrkdwn, which the web
  side converts as its `post` would. The channel row is written only
  when the store lacks it, so the bot's row (name, archived) stands. A
  mirror failure is printed and the post stands.
- Standalone (`"frontend": "web"`), `chat_notify.post` and `post_as_bot`
  post each part into the store as the bot through the writer-only web
  front end, all parts of a post in one new thread, in the channel the
  `notify` entry names, else the EA channel (`main`); the EA channel and
  the DM are made when the bot has not made them yet, and another
  channel must exist. A process that is not the owner's posts nothing
  and says so. `chat_upload.py` in such a process stages its job in the
  outbox instead; the bot's drain finds the channel that holds a web
  thread key through the store (`WebFrontend.thread_channel`) and posts
  there when it is one of the owner's.
- Not mirrored: a Slack webhook post (no message id comes back), and a
  Zulip direct message or scheduled direct message from
  `chat/direct.py` (they live outside the streams the web chat holds; the
  Zulip server delivers a scheduled one later).

**The bot's side.** The serving `WebFrontend` runs a tail thread that
reads `SELECT MAX(seq) FROM events` every `tail_s` seconds (1) and hands
the events after the last seq it delivered to the open streams, in seq
order and with the payload as logged: the JSON the in-process path
sends. The bot's own writes go out at once; when another process logged
events before one of them, those go first, read back from the log. The
store keeps the last seq it handed on, so no event goes out twice (a
stream also skips any seq at or below its last). With no stream open the
new events are passed over: a stream that opens later replays the log.

**SQLite.** One file, the default rollback journal and a 10-second busy
wait on each connection. Writers take turns, so the seqs commit in
order; the journal takes the database file's mode. A file id the store
mints (`f<n>`, standalone only) is inserted before the bytes take its
name, and minted again when another process took it.

**Permissions.** The bot and every sender that writes the store run as
the owner, so the 0600 store, its journal and `webchat-files/` need no
change. In Docker the bot and scheduler containers share the
`ea-private` volume. Standalone (`"frontend": "web"`), a command-line
sender gets the bare `WebFrontend`, writer-only, on the store its own
process computes; only an owner process's is the bot's (the senders
above check).

## The web app (B)

A single-page app, phone-first, installable (manifest + a service
worker that caches the static files only, never API responses).
No framework, no build step, no CDN; everything under `webui/`.

Layout: a channel list (left on wide screens, a drawer on narrow), a
thread list for the selected channel, and the open thread with a
composer at the bottom. The DM channel opens by default. Unread badges
from the event stream. A top-level message in a channel starts a thread;
replying stays in it.

Rendering: the markdown subset above, escaped first; reaction chips
inline after the message; cards as a bordered block with title, fields
(label: value), the body in a scrollable monospace block, the quoted
thread collapsed under a toggle, buttons for actions (the primary one
emphasised, cancel plain) and a dropdown per select; a `status` card
shows its final line and no buttons. Files render as a link with the
name and size, images inline.

Live updates: `EventSource(base + "/api/events")`, replay from the last
seen seq, reconnect handled by the browser. Composer: Enter sends,
Shift+Enter newline; a paperclip button posts to `/api/files`. Pairing:
on 401 show a token field that posts to `/api/pair`.

Every call goes through one transport interface (`api(method, path,
body)`, `events(since, sink)`). A browser paired with the relay uses it
when the base path does not answer (off the tailnet, or on the relay's
copy of the app); the relay transport, the pairing link (`#tr1=`), the
"Pair a phone" entry in the drawer and the QR encoder (`qr.js`) are in
`docs/webchat-relay.md` (For the web app).

Dark and light from `prefers-color-scheme`. Keep the whole app under
~2,500 lines with the relay transport and the QR encoder; readable by one
maintainer.

Installing on a phone. The manifest (`display: standalone`, `scope` and
`start_url` `./`, so the base path with its slash, since the page links
the manifest by relative path) names 192 and 512 icons and a maskable 512
whose glyph keeps inside the 40% safe circle; `apple-touch-icon` is an
opaque 180×180 PNG. The head carries the `apple-mobile-web-app-*` tags
(status bar style `default`, so the clock stays readable in both themes),
a `theme-color` per scheme matching the header, and a viewport with
`viewport-fit=cover` and `interactive-widget=resizes-content`. Headers,
the drawer, the lists and the composer pad by `env(safe-area-inset-*)`.
There are no startup images: `app.css`, the first stylesheet, paints the
background before anything else (inline style is outside the CSP), and
the manifest's `background_color` is the dark theme's. Outside the
installed app (`navigator.standalone` false on iOS, `display-mode:
browser` elsewhere) a line at the drawer's foot says how to install: on
iOS Share, then Add to Home Screen; in Chrome an Install button for its
`beforeinstallprompt`. Hiding it lasts 30 days; the time it was hidden
is the app's only use of `localStorage`. A link to a route of this app
(`<base>/#/...`, absolute or relative) opens in it; any other keeps
`target="_blank"`, so the installed app hands it to the browser. On iOS
the keyboard shrinks only the visual viewport: the app sets its height
from `visualViewport` (not while zoomed in) and keeps the thread at its
newest message while the pane shrinks. The page does not overscroll, so
no pull-to-refresh.

## Implementation notes (A)

What `botcore/botcore/chat/web_frontend.py` does where the contract
leaves a choice. Additive only: no field, route or key above changes.

**Channels and ids.** Standalone, the EA channel is `main` (name `ea`)
and the DM is `dm` (name `dm`); there is no scheduling channel. Rooms
are `r<n>`, files `f<n>`, messages `w<seq>`, threads `t<seq>`. A channel
row also carries `description`. `unread` counts the bot's messages after
the owner's last message in the channel or the channel's read mark;
`GET .../threads` and `GET .../threads/{key}` move the read mark to the
newest message. `thread_channel(key)` names the one channel that holds a
thread key or message id (None for none or several), for a caller that
has the key alone (the bot delivering a staged job).

**Thread list.** Threads come newest first by their first message. Each
item is that message with `reply_count` and `last_at` (the thread's
newest post); `before=<seq>` is the smallest first-message `seq` the app
holds, and the page holds the threads that opened before it.

**Times.** A new row's `updated_at` equals its `posted_at`; an update, a
card update and a status line raise it, a reaction leaves it. The app
keeps the row with the larger `updated_at` and reads a row whose two
times differ by 2 seconds or more as an edit, not as new.

**`/api/me`** also returns `base_path`.

**Text.** The bot's text (`post`, `update`, `mirror_post`,
`mirror_update`, an upload's comment) is converted with
`chat.text.mrkdwn_to_zulip`, as on Zulip, except that emoji shortcodes
keep their Slack names, the vocabulary reactions use; the app knows both
names. The owner's text is stored as typed. A `status` row's text is the
core's status line as written (Slack mrkdwn), which the app renders as
card text. A card is stored as it is: its strings come from the model
and from mail, may hold the inert-span sentinels U+E000 and U+E001
(`card_text.inert`; the span between them is literal text), and are
never markup.

**Card clicks.** The `value` the core receives is the one stored with
the card; a `value` from the browser is ignored. For a select,
`selected` must be one of the card's option values. A click on a card
already replaced by its status line is a 400.

**Auth.** The `Tailscale-User-Login` header counts only when
`tailscale_login` is set, `trust_tailscale_header` is on, the peer is
loopback and the peer's socket is tailscaled's (Identity and auth).
`start()` writes the pairing token file (mode 0600) when there is none,
then checks it. The cookie's `Path` is `base_path`. Pairing reads at
most 1 KiB, and one attempt runs at a time (a wrong token waits
`pair_delay_s` under the lock; a request that waits 10 s for the lock is
a 429). Errors are 400, 401 and 404; 421 for a wrong `Host`; 403 for
another site, a POST without this app's `Origin`, and a card action,
push subscription or revoke from a device paired on the relay's origin
below `"all"` (`X-Tacitus-Relay-Cards: deny`), and a relayed request
(`X-Tacitus-Relay`) to `/api/pair` or `/api/relay/`; 429 for pairing; 503
when the relay cannot open a pairing; and 500 for an unexpected failure.

**Bodies.** A body has a `Content-Length` of digits only, or is chunked
(no other `Transfer-Encoding`): each chunk size is 1 to 8 hex digits, and
a chunk that would pass the route's limit is refused before it is read.
An error answer reads at most 64 KiB of an unread body (1 KiB on the
pairing route) and leaves a larger one unread.

**Event stream.** The stream opens with `retry: 3000`. With neither
`Last-Event-ID` nor `since` it sends only new events; with both,
`Last-Event-ID` wins. A seq beyond the log's end (a store that was reset)
replays nothing and the stream goes on from the end. A stream that
falls 2,000 events behind is closed, and the browser reconnects with
`Last-Event-ID`. Events are kept 14 days.

**Static files.** `webui/` is served with `Cache-Control: no-cache`. An
HTML page carries `Content-Security-Policy` with `script-src 'self';
style-src 'self'` and `form-action 'none'` (no inline
script, no inline style, nothing from another origin; with a relay
configured, `connect-src` also names the relay's origin, so the app can
reach the relay when this server cannot be reached). Every answer,
static or API, carries `Cross-Origin-Resource-Policy: same-origin`. `<base_path>`
answers 301 to `<base_path>/`. A path whose last part has no extension
serves `index.html`. With no `webui/index.html` a placeholder page
answers. `botcore/pyproject.toml` ships `webui/*` as package data.

**Files.** A file kept here has its `id` and the `url`
`<base_path>/api/files/<encoded id>`; a mirrored file kept only on the
primary has an empty `id` and the primary's absolute `url`.
`GET /api/files/{id}` answers with `Content-Disposition: attachment` and
`Content-Security-Policy: sandbox`; an `<img>` still renders it. Uploads
are at most 50 MB; message text at most 20,000 characters.

**Links the core writes.** `room_url` is `<site>/#/<channel>` and
`permalink` is `<site>/#/<channel>/<thread_key>/<msg_id>`, where site is
`public_url` or `base_path` and each part is URL-encoded. The app can
route these hashes. `mention` is `[#name](room_url)`.

**Prompt wording.** `platform` is `Tacitus web chat`, with no article:
the prompts use it as an adjective ("a real-time {platform} message").

**Shadow mode hooks (for C).**

- Set `web.inbound` before `web.start()`; `start()` makes the standalone
  channels only when `inbound` is None.
- `inbound(ev)` receives an `InboundMessage` or an `ActionEvent` and runs
  under the frontend's dispatch lock. The message has `msg_id` "",
  `user_id` "owner", `thread_key` None for a new thread (else the stored
  key, which is the primary's), `is_dm`, and `raw` `{"source": "web"}`.
  An upload also has `subtype` "file_share", `files` [FileRef(name,
  mimetype)] and `raw["path"]`, a temp file holding the bytes, deleted
  after the call. For a message or an upload `inbound` returns the
  primary's `MessageRef`; the browser gets the row stored under it. When
  the composite has not mirrored that row, the web frontend stores it
  (author owner). For an upload whose stored row has no bytes here, it
  keeps them under the row's file id, or under `<channel>:<message id>`
  when the row's file has none. A click's `card` ref is the stored one; its return
  value is ignored.
- Mirror the owner's text without the `**You (web):** ` prefix.
- `mirror_file(ref, file, author="bot", *, path=None, text="")`: `path`
  and `text` are optional keywords; with `path` the bytes are kept under
  `file.id` and served from `/api/files/`; without it the stored file has
  no id and its link is `file.url`, which should be absolute.
- `mirror_post`, `mirror_update`, `mirror_card`, `mirror_card_status`
  and `mirror_file` return the stored message JSON, `mirror_channel` the
  channel row, `mirror_reaction` True when the message is stored. A
  mirror into a channel not yet described makes a placeholder row (kind
  `room`, name the id) that `mirror_channel` corrects.
- Keywords for the history copy: `mirror_post` and `mirror_file` take
  `quiet=True` (a history row: no event, `history` 1, None returned for
  a row stored already or older than its thread's newest) and
  `markdown=True` (the bot's text is Markdown already, as Zulip's
  history is, and is stored as given). `mirror_channel` takes
  `description=`; None keeps the stored one. A store from before the
  `history` column gains it on open, its rows all live ones.
- `stop()` stops the server and the tail thread and closes the open
  streams; `url` is the loopback URL with the base path.

## Notifications (Web Push)

`botcore/botcore/chat/web_push.py` sends a notification to every browser
that turned on the drawer's **Notifications** toggle, also with the app
closed. It needs the `cryptography` package (installed with msal); without
it `/api/me` says `"push": false`, the app hides the toggle and nothing
else changes. No other dependency.

**What is pushed.** A new card (`post_card`, or `mirror_card` for a row
not stored yet), at urgency `high`: "Tacitus" and the card's title. Never
a field, a line or the body: they hold addresses and mail text, and a
notification shows on a locked screen. A new bot message in a `dm` channel (`post`, or `mirror_post` for
a new row by the bot), at urgency `normal`: "Tacitus" and the first line
of the text. A loud alert: `chat.direct.web_notify` (alert.py, next to
the direct message, and page_watch.py) at urgency `high`. Edits, card
updates, status lines, reactions and the owner's own messages are never
pushed. The text loses its emoji shortcodes and Markdown marks
(`web_push.plain_line`); the title is at most 120 characters and the body
240.

**Payload.** JSON `{"title", "body", "url", "tag"}`, encrypted to the
subscription (RFC 8291, `aes128gcm` of RFC 8188, one 4096-byte record),
padded after the delimiter to a multiple of 256 bytes.
`url` is the message's web permalink; `tag` is `<channel>/<thread key>`,
so the device keeps one notification per thread and a new one alerts
again. TTL is one hour.

**Rate.** At most one push per thread in 30 seconds, counted apart for
`high` urgency, so a card is never held back by a message push in its
thread (`Pusher.notify`).

**VAPID.** The key pair (P-256) is made on first use in
`<store dir>/webchat-vapid.json`, mode 0600; when two processes race, the
first file stands. Each push carries `Authorization: vapid t=<ES256 JWT>,
k=<public key>`; the JWT's `aud` is the endpoint's origin, `exp` twelve
hours ahead, `sub` the web block's optional `push_subject` (a `mailto:`
or `https:` URL), else the project's URL, so the tailnet host name does
not go to the push services. A replaced key file breaks the browsers'
subscriptions: the app drops one made with another key at start, and the
owner turns the toggle on again.

**Subscriptions.** The `push_subscriptions` table (`endpoint` primary
key, `keys` JSON, `created_at`, `last_ok`, `failures`), at most 20 (the
ones longest without a delivery go first). `subscribe` takes only an
https endpoint on a known push service, by exact host
(`web_push.PUSH_HOSTS`: `fcm.googleapis.com`,
`updates.push.services.mozilla.com`, `web.push.apple.com`; and
`PUSH_HOST_PATTERNS`: `jmt<n>.google.com` and one name under
`notify.windows.com`), with a P-256 key and a 16-byte auth secret; the
bot follows no redirect, so a browser cannot steer it to another host.
Each row also keeps `last_seen`, the last time the app posted it. Before
a send, a list or a subscription, `Pusher.prune` drops every row when
the pairing token's hash differs from the one the store recorded (the
`meta` table; the first hash a store sees is recorded with its rows
kept, and a process that cannot read the token changes nothing), and
each row the app has not posted for 30 days. A request the relay marks `X-Tacitus-Relay-Cards: deny` (a device
paired through the relay's origin below `"all"`) is refused with 403: the
relay serves that page's code, and a subscription outlives the pairing.
On sending, 404 and 410 drop the subscription, 429 holds that endpoint
for its `Retry-After` (60 s by default), and any other failure raises
`failures` and is logged with the host only, never the endpoint path.

**Sending.** `WebFrontend.notify(title, body, url, tag, *, urgency, at,
wait)` never raises. The push worker thread (`start()` starts it,
`stop()` ends it) sends queued pushes. `at`, a Unix time ahead, puts the
push in the `push_queue` table under its tag (a later one with the same
tag replaces it; `cancel_notify(tag)` drops it); the worker looks every
15 seconds, claims due rows in one transaction, and drops one more than
the TTL late. `wait=True` sends before returning: alert.py's process
builds the web front end (`chat.load_web_frontend`, not started) and
sends or queues through it, and does nothing when the chat block runs no
web front end or its store does not exist. A queued alert therefore goes
out while the bot runs; the Zulip direct message, held by the Zulip
server, does not need the bot.

**The app.** The toggle sits at the foot of the channel drawer and shows
once `/api/me` says `push`. Under it, the list of subscribed browsers
(host and date) has a Revoke button each; this browser's own is the
toggle. Turning it on calls
`pushManager.subscribe({userVisibleOnly: true, applicationServerKey})`
straight from the tap (Safari asks for the permission only then) and
posts the subscription; off unsubscribes on both sides. At start the app
posts a subscription the browser already holds, so a reset store learns
it again. iOS delivers Web Push only to the app added to the Home Screen:
where `navigator.standalone` is `false` (iOS Safari) the drawer's install
line says so. `sw.js` shows each push (`showNotification` with the title,
body, icon, tag and `data.url`) and on a tap focuses the app's window last
in front and navigates it to the url, else opens one; a window that
`navigate()` cannot move gets `{open: url}` as a message and routes to it
when it is one of the app's routes. A url outside the app's scope opens the
app's start page.
