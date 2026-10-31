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
    "pair_token_file": "<private_dir>/webchat-pair.token"
  }
}
```

`"frontend": "web"` with no `shadow` runs the web front end alone. An
optional `push_subject` key (a `mailto:` or `https:` URL) is the contact
the push services see (Notifications, below).
`"shadow": "web"` with any primary wraps the primary in `ShadowFrontend`.
`FRONTENDS` in `preprocessor/chat/__init__.py` gains `"web"`.
`get_chat` defaults every `web` key (port 8470, base_path `/chat`, bind
loopback). The store is `<private_dir>/webchat.sqlite` (owner process) or
`<ea_dir>/state/webchat.sqlite` otherwise; a `store_path` key overrides.

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
  posted_at REAL, updated_at REAL, UNIQUE(channel, id))`.
  `author` is `bot` or `owner`; `kind` is `message`, `card`, `status`
  (a card replaced by its final line) or `file`; `card`/`file`/`reactions`
  are JSON text or NULL.
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
| GET | `/api/me` | `{"owner": "...", "bot": "...", "public_url": ..., "push": bool}` |
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
Each writes the row and appends the event.

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
  (main, sched, DM and the registered rooms from `archived_map()`).
  `run_forever()` runs the shadow's server thread and the primary's loop.
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
  (`author = "owner"`) before the core handles it; an `ActionEvent` is
  passed through.
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
`shadow`; `"frontend": "web"` returns a bare `WebFrontend`.

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
Errors are 400, 401 and 404, 403 for a push subscription from a device
paired on the relay's origin (Notifications, below), and 500 for an
unexpected failure.

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
- `stop()` stops the server and closes the open streams; `url` is the
  loopback URL with the base path.

## Notifications (Web Push)

`botcore/botcore/chat/web_push.py` sends a notification to every browser
that turned on the drawer's **Notifications** toggle, also with the app
closed. It needs the `cryptography` package (installed with msal); without
it `/api/me` says `"push": false`, the app hides the toggle and nothing
else changes. No other dependency.

**What is pushed.** A new card (`post_card`, or `mirror_card` for a row
not stored yet), at urgency `high`: the card's title, and its first line
(a line of the card, else its first field as `Label: value`, else the
body). A new bot message in a `dm` channel (`post`, or `mirror_post` for
a new row by the bot), at urgency `normal`: "Tacitus" and the first line
of the text. A loud alert: `chat.direct.web_notify` (alert.py, next to
the direct message, and page_watch.py) at urgency `high`. Edits, card
updates, status lines, reactions and the owner's own messages are never
pushed. The text loses its emoji shortcodes and Markdown marks
(`web_push.plain_line`); the title is at most 120 characters and the body
240.

**Payload.** JSON `{"title", "body", "url", "tag"}`, encrypted to the
subscription (RFC 8291, `aes128gcm` of RFC 8188, one 4096-byte record).
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
https endpoint on a known push service (`web_push.PUSH_HOSTS`: Google,
Mozilla, Apple, Microsoft), with a P-256 key and a 16-byte auth secret;
the bot follows no redirect, so a browser cannot steer it to another
host. A request the relay marks `X-Tacitus-Relay-Cards: deny` (a device
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
once `/api/me` says `push`. Turning it on calls
`pushManager.subscribe({userVisibleOnly: true, applicationServerKey})`
straight from the tap (Safari asks for the permission only then) and
posts the subscription; off unsubscribes on both sides. At start the app
posts a subscription the browser already holds, so a reset store learns
it again. iOS delivers Web Push only to the app added to the Home Screen:
where `navigator.standalone` is `false` (iOS Safari) the drawer says so.
`sw.js` shows each push (`showNotification` with the title, body, icon,
tag and `data.url`) and on a tap focuses an open window of the app and
navigates it to the url, else opens one; a url outside the app's scope
opens the app's start page.
