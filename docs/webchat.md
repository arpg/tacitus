# Tacitus web chat: the contract (v0, 2026-10-30)

Tacitus ships its own chat interface so an install needs no Zulip or
Slack. The bot process serves a small web app (a PWA) over HTTP on
loopback; the owner reaches it over their tailnet (`tailscale serve`
proxies a path to the port and adds identity headers) or, later, through
a Tacitus relay. In **shadow mode** the bot keeps its primary front end
(Zulip today) and mirrors every post, update, card and reaction into the
web front end, and accepts input from either side, so the owner can try
the new interface against live traffic.

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
`preprocessor/ea_config.get_chat`; `ea-config.json` otherwise):

```json
"chat": {
  "frontend": "zulip",
  "shadow": "web",
  "web": {
    "bind": "127.0.0.1",
    "port": 8470,
    "base_path": "/chat",
    "public_url": "https://<host>.ts.net/chat",
    "tailscale_login": "<owner login as tailscale serve reports it>",
    "pair_token_file": "<private_dir>/webchat-pair.token",
    "backfill_days": 7
  }
}
```

`"frontend": "web"` with no `shadow` runs the web front end alone.
`"shadow": "web"` with any primary wraps the primary in `ShadowFrontend`.
`FRONTENDS` in `preprocessor/chat/__init__.py` gains `"web"`.
`get_chat` defaults every `web` key (port 8470, base_path `/chat`, bind
loopback, backfill_days 7). The store is `<private_dir>/webchat.sqlite`
(owner process) or `<ea_dir>/state/webchat.sqlite` otherwise; a
`store_path` key overrides.
`backfill_days` is the history a shadow copies in when the bot starts
(see Shadow mode); 0 copies none.

## Identity and auth

One owner, one bot. Standalone, `owner_id = "owner"` and
`bot_user_id = "bot"`. A request is the owner when either holds:

1. Header `Tailscale-User-Login` equals `tailscale_login` (tailscale
   serve sets it and strips a client-supplied one; the server binds
   loopback so nothing else reaches it), or
2. Cookie `ea_web` equals the pair token. `POST /api/pair` with
   `{"token": "..."}` matching `pair_token_file` sets the cookie
   (HttpOnly, SameSite=Strict, Secure when `public_url` is https).

Anything else: 401 JSON `{"error": "not the owner"}`. Static files are
served without auth; every `/api/` route needs it.

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
| GET | `/api/me` | `{"owner": "...", "bot": "...", "public_url": ...}` |
| GET | `/api/channels` | `[{"id","name","kind","archived","last_at","unread"}]` |
| GET | `/api/channels/{ch}/threads?limit=50&before=<seq>` | thread parents, newest first, each with `reply_count` and `last_at` |
| GET | `/api/channels/{ch}/threads/{thread_key}` | the thread, oldest first, parent first |
| POST | `/api/channels/{ch}/messages` | `{"text", "thread_key"?}` → the stored owner message; dispatches an `InboundMessage` |
| POST | `/api/channels/{ch}/files` | multipart `file`, optional `text`, `thread_key` → the stored `file` message; dispatches an `InboundMessage` with `files=[FileRef]` |
| GET | `/api/files/{id}` | the bytes, `Content-Disposition: attachment` |
| POST | `/api/cards/{ch}/{id}/actions` | `{"action_id", "value"?, "selected"?}` → `{"ok": true}`; dispatches an `ActionEvent` (`source` `button` or `select`) |
| GET | `/api/events` | SSE; `Last-Event-ID` or `?since=<seq>` replays from that seq; `id:` is the event seq; `event:` is the type; `data:` is the message JSON (`channel` events carry the channel row); a `: ping` comment every 20 s |
| POST | `/api/pair` | `{"token"}` → sets the cookie |

Channel ids in URLs are URL-encoded (Zulip ids carry characters). A
thread_key is likewise encoded.

Errors: JSON `{"error": "..."}` with 400/401/404. The server is
`http.server.ThreadingHTTPServer` on a daemon thread started by
`start()`; `run_forever()` blocks on a stop event; SSE handlers block on a
per-connection `queue.Queue` fed by the store's event fan-out.

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
  and time, author `owner` for the primary's owner id (and for a
  `**You (web):**` post) else `bot`; a card goes in as the text the
  primary shows. A quiet row has no event and never counts as unread, a
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
  web row is stored under the primary id as `author = "owner"`. A click
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
holds no chat credential: `chat_upload.py` run as ea-bot stages its job
(`slack_outbox`), and the bot sends it through the composite, which
mirrors it.

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
process computes.

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

Dark and light from `prefers-color-scheme`. Keep the whole app under
~1,500 lines; readable by one maintainer.

## Implementation notes (A)

What `botcore/botcore/chat/web_frontend.py` does where the contract
leaves a choice. Additive only: no field, route or key above changes.

**Channels and ids.** Standalone, the EA channel is `main` (name `ea`)
and the DM is `dm` (name `dm`); there is no scheduling channel. Rooms
are `r<n>`, files `f<n>`, messages `w<seq>`, threads `t<seq>`. A channel
row also carries `description`. `unread` counts the bot's messages after
the owner's last message in the channel or the channel's read mark;
`GET .../threads` and `GET .../threads/{key}` move the read mark to the
newest message.

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
`tailscale_login` is set and the peer is loopback. `start()` writes the
pairing token file (mode 0600) when there is none. The cookie's `Path`
is `base_path`. A POST whose `Origin` is not the request's host (or
`public_url`'s, or a loopback proxy's `X-Forwarded-Host`) is a 401.
Errors are 400, 401 and 404, and 500 for an unexpected failure.

**Event stream.** The stream opens with `retry: 3000`. With neither
`Last-Event-ID` nor `since` it sends only new events; with both,
`Last-Event-ID` wins. A seq beyond the log's end (a store that was reset)
replays nothing and the stream goes on from the end. A stream that
falls 2,000 events behind is closed, and the browser reconnects with
`Last-Event-ID`. Events are kept 14 days.

**Static files.** `webui/` is served with `Cache-Control: no-cache`. An
HTML page carries `Content-Security-Policy` with `script-src 'self';
style-src 'self'` (no inline
script, no inline style, nothing from another origin). `<base_path>`
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
