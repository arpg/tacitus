# Web chat relay

**Status:** wired (2026-10-31). The web front end starts the relay client
when its config holds a `relay` block, and the web app pairs and talks
through it. No relay is deployed: turning it on is config (Owner steps).
This is part D of the Tacitus web chat: A is the web front end
(`botcore/botcore/chat/web_frontend.py`), B the web app
(`botcore/botcore/chat/webui/`), C shadow mode
(`botcore/botcore/chat/shadow_frontend.py`).

| File | What it is |
|---|---|
| `relay/relay_server.py` | The relay: one standalone stdlib script, no repository imports |
| `botcore/botcore/chat/relay_client.py` | The bot side: `start_relay`, `RelayClient`, the key file, the owner CLI |
| `botcore/botcore/chat/relay_crypto.py` | The protocol with no I/O: pairing, handshake, sealed frames |
| `botcore/botcore/chat/webui/app.js` | The app's relay transport: the same protocol in WebCrypto, the pairing, "Pair a phone" |
| `botcore/botcore/chat/webui/qr.js` | The QR encoder for the pairing link (byte mode, level M, versions 1 to 40) |
| `botcore/botcore/chat/webui/bundle.js`, `sw.js` | The check of the app's code against the bot's signed manifest (docs/webchat.md, Code integrity) |
| `botcore/botcore/chat/web_bundle.py` | The bot's side of that check: the manifest of `webui/`, signed at start |
| `botcore/botcore/chat/relay_qr.py` | The same encoder in Python, for the CLI's QR code on a terminal |
| `scripts/webchat-relay.py` | The owner CLI: `init`, `show`, `pair`, `approve`, `revoke`, `rotate` |
| `relay/install.sh`, `relay/snippets/` | The operator's install: a pinned commit, a confined unit, the nginx blocks |
| `tests/test_relay.py` | Crypto, relay rules, the web front end's routes, a loopback run of the whole path, each attack the security review found, and rotation |
| `tests/test_web_bundle.py` | The signed manifest, its routes, the signing key in the key file, and the attestation |
| `tests/test_relay_install.py` | The install script's checks, the unit it writes, the nginx rules |
| `tests/fixtures/relay_vectors.json` | The protocol with fixed keys, made by `relay_crypto`; the app reproduces it in a browser (`tests/test_webui.py`) |

## What it is for

The owner reaches the web chat over the tailnet, or on the bot's own
machine (`http://127.0.0.1:8470/chat/`) with no Tailscale at all. A phone
off the tailnet (on cellular, without Tailscale, or on a managed device
that cannot run it) reaches it through the relay: a small public service
that Tacitus runs. The relay must never be able to read a message. An install's mail
content passes through it only as ciphertext, and the threat model
(`docs/SECURITY.md`) treats the relay operator as honest but curious at
best and possibly compromised.

## The model

```
 phone (browser)              relay (public, TLS)            bot host (the owner's)
 web app + device key         routes by install id           RelayClient thread
                                                             (outbound only)
 seal a request  ── POST /r/I/c ──▶ queue the frame  ◀── POST /r/I/bot ── long-poll, 25 s
                                    hand it to the poll ──────────────▶ open it, replay it on
                                                                        http://127.0.0.1:8470/chat
 open the answer ◀── 200 + frame ── match it by id   ◀── POST /r/I/bot ── seal the answer
```

The bot dials out, as Home Assistant Cloud does: no port opens on the
owner's network, and the owner needs no DNS name or certificate. The
relay matches the phone and the bot by install id and forwards frames
that are sealed end to end between the two. Home Assistant's remote UI
passes the TLS connection through to the instance; here the relay
terminates TLS, and the end-to-end layer sits inside it, so the relay can
run behind any ordinary TLS terminator.

The relay sees install ids, frame kinds, frame sizes (to 256 bytes),
timing, and random session and frame ids. It holds no key that opens a
frame.

## Where the app's code comes from

This is the part of the design that decides what a compromised relay can
do. End-to-end encryption in a web page protects only against parties
that do not deliver the page's code. A relay that serves the app can
serve a changed app, and a changed app reads everything the phone shows.
No cryptography inside the page changes that. So the phone gets its code
in one of two ways, and the bot records which at pairing.

**Own origin (the default on the tailnet).**
The owner pairs while the phone is on the tailnet. The pairing link
opens the app from the bot's own origin (`public_url`, the tailnet
address), and B's service worker caches the static files there.
Off the tailnet, the cached app starts from the service worker (a fetch
that has not answered in four seconds gives way to the cached copy) and
calls the relay cross-origin. The relay answers CORS on `/c` and `/sse` with no
credentials, since the client key header and the sealed frames are the
only authentication. The relay never serves code to this device. A new
release reaches it off the tailnet from the bot: the page fetches the
signed manifest (`/api/bundle`) and each changed file (`/api/app/<file>`)
in sealed frames, as it fetches messages, and the page and its service
worker check each file against the bot's signature before the worker
keeps it (docs/webchat.md, Code integrity). A compromised relay sees
ciphertext and nothing else: it can hold an update back, but not change
one.

**Relay origin ("Pair through the relay").** For a phone that never
joins the tailnet, and for an install with no Tailscale at all, a relay
on a hostname of its own serves a copy of the app (`--static`, which
`relay/install.sh --with-app` sets). The owner chooses it in "Pair a
phone" ("Pair through the relay"), which the dialog offers once the bot
signs its app bundle; `webchat-relay.py pair --relay-origin` does the same
on a terminal. That pairing link opens the relay's copy (`a`, the relay
URL as a folder) and carries the bundle hash (`b`): the sha256 hex of the
canonical manifest of `webui/` that the bot signs
(`WebFrontend.bundle_hash()`; docs/webchat.md, Code integrity). The QR
carries the hash from the owner's screen to the phone, past the relay.
The bot records the device as `origin: "relay"` with its `bundle`.
`relay_origin_access` may stay `"off"`: this pairing does not use it.

The relay's copy of the app is a bootstrap. It refuses a link without
`b`, and stops with "Code verification unavailable" before it makes a key
when it has no `verifyBundle`. Once the device is approved, at every
start and before the chat runs, the page fetches the bot's signed
manifest through the sealed channel, checks it against the bundle hash
from the QR code (then against the key that signed it), and its service
worker checks each file of the copy against it. When each file matches,
the phone posts the bundle hash to `/api/relay/attest`, and the bot
records `bundle_verified` and its release for the device. A mismatch
shows "Tacitus stopped" with the file that differs, and the phone attests
nothing.

**What such a device may do.** Once approved, it reads and chats
(`"chat"`: everything but the card actions), whether or not it attested.
The record of its check grants nothing; the list under "Pair a phone"
shows it ("verified copy of v1.2.3", or "code not verified"). The device
presses card buttons (Send, Create, Publish) only when the owner allows
them for that device: the "Allow card buttons on this phone" box beside
it under "Pair a phone" (off by default; Approve sends it, and on an
approved phone it approves again), or `webchat-relay.py approve --device
<id> --cards`. A device that waits for approval gets nothing.

**What the check buys.** It stops a copy of the app that is not the
bot's: a copy from another release (the relay and the bot out of step,
Running the relay), a changed file, a stale copy. It does not stop a
relay that serves changed code on purpose from the first load: that code
can skip the check and post the bundle hash anyway (the attestation is a
claim that the phone's code makes, and the hash is public), and the
browser installs a new service worker whenever the relay sends one. So a
verified relay-origin device still runs code that a dishonest relay can
choose: that code reads what the phone shows and chats as the owner, and
the check alone never lets it press a card button. The six-digit code
and Approve still keep the relay from pairing a phone of its own, and the
list shows where each phone's code comes from, so the owner can remove a
relay copy at once.

Plainly: pairing through the relay trusts the relay's copy of the app at
pairing time and at each update of the service worker. The owner
chooses, for each such device, whether it may press card buttons.
Pairing on the tailnet (own origin) has no such trust. A check outside
the page (Later work) would close this.

A relay-origin pairing without a bundle hash (from before the signed
bundle) is the older kind: the bot refuses it unless the owner-private
`chat.json` sets `relay_origin_access` (default `"off"`), and the current
app refuses a link to the relay's copy that holds no hash. Let in, such a
device gets that level: `"read"` is GET only (and the attestation),
`"chat"` everything but the card actions, and `"all"` lifts the limit as
the owner's explicit choice. Below `"all"` it presses card buttons only
when the owner allows them for it.

The bot records `"own"` only when the link's app URL has the origin of
the web block's `public_url`, compared normalized: the scheme, the host
lowercased and without a trailing dot, and the port written out
(`https://host:443` is `https://host`). Any other app URL, the relay's
origin under any spelling, a URL with a backslash or user info, or a
bot with no `public_url`, makes a `"relay"` device, which `new_pairing`
refuses unless asked for one; with a bundle hash the app URL must also
have the relay's own origin (`on_relay_origin`).

The origin is a property of the device key, not a claim the phone makes.
The pairing secret travels in the fragment of a link that opens one
origin, so only a page on that origin reads it, and the device's private
key then lives in that origin's storage. A relay-origin page cannot read
the storage of the own-origin app (same-origin policy).

## Pairing

1. In the web chat the owner opens "Pair a phone" (in the channel
   drawer, shown when the app reaches the bot directly and `/api/me` says
   `relay`): on the tailnet, or on the bot's own machine (a loopback
   page, signed in with the pairing token). On the bot's own origin (the
   page has `public_url`'s origin and is a secure context) the dialog
   opens an own-origin pairing at once (`POST /api/relay/pair` with `{}`)
   and offers "Pair through the relay" for a phone without Tailscale. On
   any other page (loopback, or a LAN address over plain http, where a
   phone has no WebCrypto) a phone could not pair on the page's origin,
   so the dialog makes no link until the owner taps "Pair through the
   relay" (`{"via": "relay"}`), which it offers when
   `/api/relay/status` says `pair_relay` (the bot has a bundle hash). A's
   route calls `RelayClient.new_pairing()` and B draws the returned string
   as a QR code on a canvas (`qr.js`), with the link as text beside it,
   what the phone may do once approved, and the paired phones below. Without the UI, `scripts/webchat-relay.py pair` draws the
   same QR code on the terminal (`relay_qr.py`; `--print-link` prints the
   text; `--relay-origin` opens the relay's copy).
2. The string is a link, `<app url>#tr2=<base64url JSON>`. The JSON holds
   `v` (2), `r` (the relay URL), `i` (the install id), `k` (the bot's
   X25519 public key), `c` (the client key), `p` (the pairing id, 16
   bytes as hex), `s` (the pairing secret, 32 bytes), `x` (the expiry,
   Unix seconds, ten minutes ahead), `a` (the app URL the link opens:
   `public_url`'s app, or `<relay url>/` for the relay's copy) and `b`
   (the bundle hash, 64 hex, when the bot has one). A link of version 1
   (`#tr1=`, `v` 1, neither `a` nor `b`) from an older bot still pairs on
   the bot's own origin. A browser never sends a fragment to a server, so
   opening the link does not put the secret in any log. The link is a
   bearer credential while it lasts: never send it through chat or mail.
3. The app reads the fragment and removes it from the address bar
   (`history.replaceState`, before anything else), and refuses a link
   whose `a` names another origin than the page's. On the bot's own
   origin it takes only `p`, `s` and `x` from the link: the relay URL,
   the install id, the bot's key and the client key come from
   `/api/relay/status`, which must answer (the phone is on the tailnet
   and signed in to the chat), and a link that names another install or
   key is refused. A link whose relay is the page's own origin is one for
   the relay's copy of the app, where no bot answers: the app refuses it
   without `b`, and stops with "Code verification unavailable" when this
   copy has no `verifyBundle`, before it makes a key. It makes its own
   X25519 key pair (WebCrypto; the private half kept as PKCS#8 bytes,
   used as a non-extractable import of them), keeps it,
   with `b` on the relay's copy, in IndexedDB before it sends the pairing
   hello through the relay (so a lost welcome leaves a key the bot
   knows), and asks before it replaces an earlier pairing; a pairing that
   fails keeps the earlier one.
4. The bot checks the hello, records the device (id: the first 8 bytes of
   SHA-256 of its public key, as hex; the name the phone gave, cleaned and
   cut to 40 characters; the origin; the bundle hash the pairing
   carried), spends the pairing and answers with a welcome. A
   relay-origin pairing without a bundle hash is refused while
   `relay_origin_access` is `"off"`, and stays unspent.
5. The new device waits for approval (`pending`, with its pairing code).
   The phone and the bot each derive the code from the hello's
   transcript hash `h` (after `c_p`): six digits of
   `SHA256("tacitus-relay/1 code" || 0x00 || h)` (the first four bytes,
   big-endian, modulo 10^6). The phone shows "This phone waits for
   approval ... code 123 456"; the bot posts a notice in the owner's DM
   with the device's name and code; "Pair a phone" lists the device with
   its code and an Approve button, and a relay-origin device with the
   "Allow card buttons on this phone" box, off by default
   (`webchat-relay.py show` and `approve [--cards]` without the UI). The
   owner approves only when the two codes match.
   Until then the device reaches nothing: every request is a sealed 403
   (`{"pending": true}`) and an `open` ends with 403. Someone else who
   spent the link first shows another code, or the owner's phone fails
   to pair: the owner removes that device instead.
6. On the relay's copy, once approved, the app checks its code before it
   opens the event stream or reads a channel, at every start (Where the
   app's code comes from): the bot's signed manifest against `b`, each
   file against the manifest, then the attestation. A mismatch stops the
   app on "Tacitus stopped" and names the file; a check that cannot run
   stops it with "Code verification unavailable". The list under "Pair a
   phone", and `show`, give each device's origin and, for a relay copy,
   "verified copy of <release>" or "code not verified" and its card
   choice (`cards_allowed`).

A pairing works once, expires in ten minutes, and at most eight are open
at a time; an expired pairing's secret leaves the key file at the next
write. A phone whose welcome was lost says a plain hello next: its key
is already registered (and waits for approval like any new one).

## Handshake and sessions

The handshake follows the Noise IK pattern (the phone knows the bot's
static key in advance, from the QR) with X25519, HKDF-SHA256 and
AES-256-GCM. It is not wire-compatible with Noise: a fixed transcript
hash and three HKDF calls are easier to write twice (Python here, WebCrypto
in B) than a Noise state machine, and need no library. `S` is a static
public key, `s` its private key, `E`/`e` an ephemeral pair, `psk` the
pairing secret (empty bytes on a plain hello).

The phone's hello:

```
header = 0x01 || mode (0 plain, 1 pairing) || pairing id (16 bytes, mode 1 only)
h   = SHA256("tacitus-relay/1" || 0x00 || install id || 0x00 || S_bot || header || E_phone)
es  = X25519(e_phone, S_bot)
c_s = AESGCM(HKDF(es || psk, salt=h, info="tacitus-relay/1 static"), nonce=0, S_phone, aad=h)
h   = SHA256(h || c_s)
ss  = X25519(s_phone, S_bot)
c_p = AESGCM(HKDF(es || ss || psk, salt=h, info="tacitus-relay/1 hello"), nonce=0,
             pad({"t": unix time, "name": ...}), aad=h)
h   = SHA256(h || c_p)
hello.c = header || E_phone || c_s || c_p
```

The bot recomputes each step. Opening `c_s` gives the phone's static key;
opening `c_p` proves the phone holds `s_phone` (and, when pairing, the
secret). It refuses a clock more than ten minutes off, an ephemeral key it
saw in the last ten minutes, and a device that is neither registered nor
spending an open pairing. Then:

```
ee   = X25519(e_bot, E_phone)
se   = X25519(e_bot, S_phone)
h    = SHA256(h || E_bot)
okm  = HKDF(es || ss || ee || se || psk, salt=h, info="tacitus-relay/1 session", 80 bytes)
k_c2b = okm[0:32]   k_b2c = okm[32:64]   sid = hex(okm[64:80])
welcome.c = E_bot || AESGCM(k_b2c, nonce=("b2c\0" || 0), pad(pack({"device", "t"})), aad)
```

The phone derives the same keys and opens the welcome. What each piece
buys:

| Property | From |
|---|---|
| The relay reads nothing | every key needs an X25519 private key the relay never holds |
| Only the bot answers | `es` needs the bot's private key; a welcome from anyone else does not open |
| Only a paired phone gets a session | `ss` in the hello, `se` in the keys |
| Only a phone that saw the QR can pair | the pairing secret is in every key of a pairing hello |
| Past sessions stay sealed if both static keys leak later | `ee`: both ephemeral keys are gone |
| A replayed hello does nothing | the clock check and the ephemeral key cache, which keeps each key for twice the allowed clock skew (a hello stamped ten minutes ahead passes the clock check for twenty) and refuses every hello while full rather than forget one; a replay that gets through after a bot restart yields a session no one holds keys for |
| The owner sees which device asks to pair | the pairing code from `h`, shown on the phone and beside the waiting device |

A hello itself is sealed under `es` and `ss` only, both of which take
the bot's static key. If that key leaks later, past hellos open (the
device's public key, its name, the clock); their sessions do not, since
those take `ee` as well.

A session lives in the bot's memory until it is idle for twelve hours or
the bot restarts (at most 32 are kept, 4 per device). The phone says
hello again on an `err` of `session` (and on a counter near 2^48).

### Frames

Every frame on the wire is one JSON object:

```json
{"id": "<frame id>", "k": "<kind>", "sid": "<session id>", "n": 7, "c": "<base64url>"}
```

A sealed frame's nonce is a 4-byte direction tag (`c2b\0` or `b2c\0`) and
the 8-byte counter `n`. Its authenticated data is
`"tacitus-relay/1|" sid "|" kind "|" id`, so the relay cannot move a body
to another request, another kind or another session. Each direction keeps
a replay window of 4096 counters (concurrent requests may arrive in any
order); a frame that fails to open does not spend its counter.

A sealed plaintext is `pack(head, body)`: a 4-byte length, a JSON object,
then the raw body bytes (empty for every kind but `req` and `res`), so a
file crosses base64 once, in the frame. Sealing pads it (a 4-byte length,
the data, zeros up to a multiple of 256 bytes).

| Kind | From | Sealed | Plaintext |
|---|---|---|---|
| `hello` | phone | handshake | above |
| `welcome` | bot | yes | `{"device", "t"}` |
| `req` | phone | yes | `{"m": "GET" or "POST", "p": "/api/...", "h": {headers}}` and the request body |
| `res` | bot | yes | `{"s": status, "h": {"content-type", "content-disposition"}}` and the response body; a ping's answer is `{"pong": t}` |
| `open` | phone | yes | `{"since": seq}` (optional): open the event stream |
| `sse` | bot | yes | one event, `{"id", "event", "data"}`; the first carries the local stream's `{"retry", "data": ""}` |
| `end` | bot | yes | `{"s": status}`: the local event stream ended |
| `ping` | phone | yes | `{}`: is the session alive |
| `err` | bot or relay | no | `{"e": code}` only: `session` (say hello again), `auth` (pair again, or a removed device's stream ends), `replay`, `bad`, `busy`, `offline`, `timeout` |
| `close` | relay | no | none: the phone's event stream went away |

Frame ids are 1 to 64 characters of `[A-Za-z0-9_-]`; the phone uses 16
random bytes as hex. The relay keeps only these fields of a frame, each
checked, and replaces an `err` code it does not know with `bad`, so it
carries nothing else in the clear.

## Transport

Plain HTTP: a long-poll for the bot, one request per phone frame, and a
chunked event stream for the phone. The bot side is stdlib (`urllib`); the
relay is stdlib (`http.server`); only the bot and the phone need
cryptography.

| Route | Who | Request | Answer |
|---|---|---|---|
| `POST /r/{install}/bot` | bot | `Authorization: Bearer <relay token>`; `{"frames": [...], "wait": s}` (at most 64 frames, wait at most 25 s) | `{"frames": [...]}`: the phone's frames, after at most `wait` seconds; 401 no token, 403 wrong token or not activated |
| `POST /r/{install}/c` | phone | `X-Relay-Key: <client key>`; one `hello`, `req` or `ping` frame | the bot's `welcome`, `res` or `err` frame; 504 with `err` `timeout` after 30 s; 503 `{"error": "unavailable"}` for a bot offline and for a wrong key alike; 413 for a `hello` or `ping` over 4 KiB; 429 over the limits |
| `POST /r/{install}/sse` | phone | `X-Relay-Key`; one `open` frame (at most 4 KiB) | `{"ticket": <32 hex>, "ttl": 30}`: the stream's ticket, good once, for 30 s; errors as `/c` |
| `GET /r/{install}/sse?t=<ticket>` | phone | the ticket | `text/event-stream`, chunked: `data: <frame JSON>` per frame, `: ping` every 20 s, closed after `end` or `err`; 503 `unavailable` for an unknown, used or late ticket. The `open` frame reaches the bot when the ticket is spent |
| `GET /healthz` | operator | | `{"ok", "installs", "streams", "queued_bytes"}` to a monitor on the relay host (a loopback peer, no `X-Forwarded-For`); `{"ok": true}` to anyone else |
| `GET /...` | phone | | the static app from `--static` (on a hostname of its own only), with a strict CSP, COOP and CORP |

The relay checks a bot's token, and a phone's key, from the headers
before it reads a body: a stranger never makes it read or parse one.
Its own errors (a bad request line, an unknown method) are JSON with the
status phrase; HEAD is a 405 with no body.

The install id in the path is 26 base32 characters of
SHA-256("tacitus-relay install\0" || relay token). The relay checks a
bot's token against the path with that hash, so it needs no table of
installs. The client key is HMAC-SHA256(relay token, "tacitus-relay
client"); the relay derives it from the token the bot polls with, and
checks a phone's `X-Relay-Key` from the headers before it reads the body:
a request without the key never makes the relay read or parse a body, and
spends none of the install's limits. A wrong key and an install that is
offline get the same answer, so a stranger who knows an install id cannot
tell whether its bot is online.

The bot keeps one long-poll open and posts each answer at once on its own
request with `wait` 0; it handles any frames the relay hands back on
those too. On a failure it backs off from 1 s, doubling to 60 s, with
jitter; a 401 or 403 waits at least 60 s.

`EventSource` cannot send a body or set a header, and a query string
lands in the TLS terminator's access log. So the phone posts the `open`
frame with its key in the header and gets a ticket, and the stream's URL
carries only that ticket, which works once and for 30 seconds. The app
reads the stream with a `fetch` and a streamed body (an `EventSource` on
the ticket URL works too). A ticket spent, the URL opens nothing, so the
app reconnects itself with a fresh `open` frame and `since` set to the
last event id it saw. The relay answers the stream before the bot has
opened its own, so the app counts the stream open at the bot's first
frame: the bot forwards the local stream's opening `retry:` block, which
the web front end writes once it holds its subscription, and no event
falls between the two.

Long-polling suits chat volume (a few frames a minute) and passes every
proxy. Each `urllib` request opens a new connection; keeping one alive
with `http.client` is a later optimisation.

## What the bot forwards

- Only `/api/...` paths under the web front end's base path, `GET` or
  `POST`, with no `.` or `..` segment (raw or percent-encoded). Never
  `/api/pair...` or `/api/relay/...`, compared decoded and lowercased (the
  local server decodes each segment before it routes, so
  `/api/%72elay/pair` is refused too): pairing the web chat and managing
  relay devices happen in the owner's own web chat only (the tailnet, or
  the bot's machine). The one exception is `/api/relay/attest`, in
  exactly that spelling: a phone's check of its app (docs/webchat.md,
  Code integrity), which a device at any level but `"off"` may post.
  Nothing else under `/api/relay/` passes. The web front end refuses
  those routes again for any request that carries `X-Tacitus-Relay`.
  Never `/api/events` as a request: the event stream opens with an
  `open` frame, and an answer that turns out to be an event stream is
  cut. A replayed request gives up after 60 seconds in all.
- From the phone's headers only `content-type`, `accept` and
  `last-event-id`, printable ASCII. The bot adds the rest itself: the owner's credential,
  which is the `ea_web` cookie from `pair_token_file` and nothing else
  (A takes `Tailscale-User-Login` only on a connection from tailscaled,
  and refuses it from any other loopback client, so the relay client
  never sends it; without a readable token file every relayed request is
  a 401); `Host`, the loopback base URL's host and port, which A requires
  of an `/api` request (else 421); on a POST, `Origin`, the loopback base
  URL's origin (`http://127.0.0.1:<port>`), which A takes from a loopback
  client without the identity header; `X-Tacitus-Relay: <device id>`, so
  A can tell a relayed request; and `X-Tacitus-Relay-Cards: allow` or
  `deny`, whether this request may confirm a card (A's card action route
  answers 403 on `deny`). It sends no `Sec-Fetch-Site`, which A refuses
  as `cross-site` or `same-site`, and the phone cannot add one.
- Back to the phone only `content-type` and `content-disposition`.
- Bodies up to 10 MiB each way (the chat's file cap), about 13.4 MiB as
  a frame.
- The local URL must be `http` on loopback; the relay URL must be `https`
  (plain `http` only on loopback, for tests).
- A device paired through the relay's origin with a bundle hash reads
  and chats once approved, attested or not; one paired there without a
  hash gets `relay_origin_access` (above; nothing at `"off"`, the
  default). Either presses card buttons only when the owner approved it
  with them (`cards`), or, without a hash, at `"all"`. One paired through
  the bot's own origin gets everything; one waiting for approval gets
  nothing.
- Neither the relay nor the loopback server can redirect the bot: a 3xx
  is an error, never followed with the relay token or the owner's cookie.

## What the relay stores and logs

In memory, per online install: the client key (derived from the token),
the last poll time, the queue of phone frames for the bot, the requests
waiting for an answer (by frame id), the open event streams, the stream
tickets not yet spent, a rate bucket and whether a phone has used it. A
route goes 60 s after the bot's last poll. Per source address, a bucket
that meters new routes (the address itself is never logged). Nothing goes
to disk. There are no accounts. A restart drops every route: bots
reconnect within seconds and phones say hello again. After a `rotate`
(Rotation, below) the bot polls under a new install id, and the old
route goes 60 s after the last poll under it.

The optional `--allow-file` lists the install ids the relay serves (the
hosted tier's activation list); it is reloaded when it changes.

What the relay logs (stderr, to the journal under the unit): one line at
start (`relay: listening on <bind>:<port> static=<dir or -> allow=<file
or ->`, and a second line when it serves the app), then one line per
request, with no frame content, no query string, no header and no
address:

```
2026-10-30T22:00:00Z POST c t7iapcqjg3nnr2a3nj6p3dmafl 200 in=812 out=1103 41ms
```

The nginx block in front keeps no access log for `/relay/` and logs its
rate refusals at `info`, so neither an install id with a client address
nor a stream's ticket lands in the host's logs.

What the bot logs of the relay: `relay: connected`, a failure once per
kind (an HTTP error as its status code only, never the relay's text),
`relay: paired device <id> (<name>); it waits for approval`, and
"hello refused" at most once a minute with a count. The same status
code, never the relay's words, is `last_error` in `/api/relay/status`.
## Limits

Every limit is per install unless marked; `relay_server.Limits` holds
them.

| Limit | Default |
|---|---|
| Frame size | 16 MiB |
| Phone frames queued for the bot | 64, and 32 MiB |
| Phone requests waiting for an answer | 32 |
| Open event streams | 4 (a fifth closes the oldest) |
| Concurrent bot polls | 4 |
| Phone frames per second | 20, burst 100 |
| A `hello`, `ping` or `open` frame | 4 KiB |
| Stream tickets not yet spent | 8, each good for 30 s |
| New installs per source address | 20, then one per 10 s |
| Online installs (global) | 1000; a full table drops the idle route (no poll open) that no phone used, else the one idle longest |
| Queued bytes (global) | 512 MiB |
| Connections (global) | 2048 |

A stranger who learns an install id cannot spend these limits: without
the client key the relay refuses every frame first, from the headers. Any
token is some install's, so without an allow file anyone can open routes:
the meter per source address slows that, and a full table makes room
instead of refusing the next bot. The source address is the peer's, or,
from a loopback peer (the TLS terminator on the same host), the last
`X-Forwarded-For` entry. The TLS terminator in front handles slow-header
clients; the relay's own socket timeout is 30 s. The nginx block adds a
limit per client address (20 requests a second, a burst of 100, 32
connections), 16 MiB bodies on `/bot` and `/c` only (8 KiB elsewhere),
and refuses a bot poll without `Authorization` and a phone POST without
`X-Relay-Key` before the relay sees it.

The bot bounds what a relay hands it, since a compromised relay can hand
it anything: at most 64 frames from one answer (48 MiB at most), at most
64 frames waiting for its eight workers (the rest dropped), no error body
read, a pause of a second after an empty answer that came early, one
event stream per frame id and four in all, and four sessions per device.
The web front end's event stream notices a client that left within a
second, so streams opened and dropped fast do not hold its 64
connections.

## What a compromised relay can and cannot do

It **cannot**:

- read a request, an answer, an event, a file, a device name or the
  clock in a hello;
- change a frame, move a sealed body to another request, kind or
  session, or replay one (the counter window and the hello cache refuse
  it);
- answer as the bot (it has no bot private key);
- pair a device of its own: the pairing secret travels only in a link's
  fragment, and every new device waits for the owner's approval by its
  code. The exception is a link that opens the relay's own origin, where
  changed code could read it; that takes the owner's choice ("Pair
  through the relay", or `pair --relay-origin`), and such a device still
  waits for approval;
- make the phone pair with another bot: on the bot's own origin the app
  takes the relay, install and keys from the bot, not from the link;
- make the phone send a message or press a button twice: the app sends
  a POST once, whatever `err` comes back;
- make a phone open a stored file as a page: through the relay a file is
  a raster image or plain bytes to download;
- make a phone that holds a checked copy of the app run a file that the
  bot did not sign: its service worker refuses the file and stops the
  app (docs/webchat.md, Code integrity), until the relay sends a new
  service worker (below);
- learn which installs are online from the outside: `/healthz` gives its
  counts only on the relay host;
- reach the bot's loopback server: only an approved device's sealed
  request gets there, and the relay holds no device key (outside the
  relay-origin case below);
- press a card's buttons (Send, Create, Publish) through an own-origin
  device, or through a relay-origin device the owner did not allow them
  (`cards`), or confirm one by a typed code (the invariant in "For the
  web front end");
- read earlier sessions after a later key leak (forward secrecy).

It **can**:

- see which installs are online and when, the addresses of the bots and
  phones that connect (at the network level, though the relay process
  does not log them), and each frame's kind, size to 256 bytes and timing;
- drop, delay or reorder frames, close streams, refuse service, and load
  the bot within the bounds above;
- forge an `err` frame, which is not sealed. The app reads one as a hint
  about the transport only: it says hello again, retries a GET once or
  shows a note (for `auth`, "the bot did not accept this phone"; for a
  POST, that the bot may have it anyway), and never drops its keys over
  one;
- keep the bot's relay token and the client key. They let it act as this
  install's bot at a relay, which it already is. `rotate` retires both
  with the install id (Rotation), so a token the relay kept names an
  install that no bot uses;
- on a phone paired through the relay's origin (only when the owner
  chose it), serve changed code when the phone loads the app, or send a
  new service worker, which the browser installs: code that skips the
  check against the bundle hash and posts the attestation anyway reads
  everything that phone shows and chats as the owner, and presses card
  buttons on a device the owner allowed them. A copy of another release,
  or one changed where the honest check looks, stops instead (Where the
  app's code comes from). A phone paired through the own origin is out
  of its reach, as long as nothing else is served on the relay's origin
  with it (the hosting rule under Running the relay).

## Config

The `relay` block sits inside the `web` block of the chat config
(`~/.ea-private/chat.json` for the bot):

```json
"web": {
  "public_url": "https://<host>.ts.net/chat",
  "pair_token_file": "<private_dir>/webchat-pair.token",
  "relay": {
    "url": "https://relay.example.net",
    "key_file": "<private_dir>/webchat-relay.json",
    "install_id": "t7iapcqjg3nnr2a3nj6p3dmafl",
    "app_url": "https://<host>.ts.net/chat/",
    "relay_origin_access": "off",
    "wait": 25,
    "enabled": true
  }
}
```

| Key | Meaning |
|---|---|
| `url` | The relay's base URL. Required. `https`. |
| `key_file` | The key file, owner-private, mode 0600. Default: `webchat-relay.json` beside the web chat's store (the owner-private directory). Created on the first start when missing and `install_id` is not set. The relay stays off when the file is not a regular file of the bot's uid with mode 0600, when its directory is writable by another user, or when it is inside the resident's tree. |
| `install_id` | Optional pin: the bot refuses to start the relay when the key file is another install's, or missing. After a `rotate`, set it to the new id. |
| `app_url` | Where an own-origin pairing link opens the app. Default: the web block's `public_url`. A device is own-origin only when this is `public_url`'s origin. Without `public_url` (no tailnet) "Pair a phone" pairs only through the relay (an own-origin pairing is a 409). |
| `relay_origin_access` | What a relay-origin device paired without a bundle hash (from before the signed bundle) may do: `"off"` (nothing; the default), `"read"` (GET only), `"chat"` (all but card actions), `"all"`. A phone paired with "Pair through the relay" (with the bundle hash) needs none of it: once approved it reads and chats. Either presses card buttons only when the owner approved it with them (`cards`), or, without a hash, at `"all"`. |
| `wait` | The long-poll's wait in seconds (default 25, at most 55; the relay caps it at 25). |
| `enabled` | `false` keeps the block but leaves the relay off. |

The key file (JSON, mode 0600, rewritten through a temp file and a rename
under an flock on `<key file>.lock`):

```json
{"v": 1, "bot_private": "<base64url, 32 bytes>", "relay_token": "<base64url, 32 bytes>",
 "relay_url": "https://relay.example.net",
 "sign_private": "<base64url, 32 bytes>", "sign_public": "<base64url, 32 bytes>",
 "devices": [{"id": "b15625d7ae6abe8d", "public": "<base64url>", "name": "phone",
              "paired_at": 1793421333, "origin": "own"},
             {"id": "4f0c2a9e81d3b7c6", "public": "<base64url>", "name": "tablet",
              "paired_at": 1793421400, "origin": "relay", "bundle": "<64 hex>",
              "bundle_verified": true}],
 "pairings": [{"id": "<32 hex>", "psk": "<base64url>", "expires": 1793421933,
               "origin": "relay", "bundle": "<64 hex>"}]}
```

A device record waiting for approval also has `"pending": true` and its
`"code"`; Approve removes both and writes `"cards_allowed"` (whether a
relay-origin device may press card buttons; only the owner sets it). A
relay-origin pairing, and the device that spends it, keep the `bundle`
hash the link carried. `sign_private` and `sign_public` are the Ed25519
key that signs the web app (docs/webchat.md, Code integrity), made on
first use. A device that attested its app also has `"bundle_verified"`,
`"attested_at"`, and, while verified, the `"attested_bundle"` hash and the
bot's `"attested_release"`; none of them grants anything. The bot refuses
a key file that group or
others can read, that another uid owns, or that is a symlink. The key
file and the `relay` block are the owner's: the resident (`ea-bot`) can read
neither, so it cannot pair a device, read a session or move the bot to
another relay. `ea_config.get_chat` keeps a `relay` block only from the
owner-private `chat.json`: one in `ea-config.json`, which the resident can
write, is dropped.

## Rotation

`webchat-relay.py rotate --key-file <key file> --devices keep|drop` gives
the key file a new relay token. The install id is a hash of the token and
the client key is an HMAC under it (Transport), so both change with it.
The bot's static key does not come from the token.

- The client key in every pairing link shown so far, which a removed
  device also kept, reaches nothing: the relay answers a phone that shows
  it `unavailable`, as it answers for an install that is offline.
- The relay knows the install by the new id. A hosted relay serves it
  only when its allow file names it (Operator steps, 6): give the
  operator the new id, and ask that the old one be removed. A relay
  without an allow file needs nothing. A `relay` block that pins
  `install_id` must name the new id; with the old one the next start
  leaves the relay off.
- `--devices keep` keeps the bot's static key and the device records,
  approved or waiting. `--devices drop` makes a new static key as well
  and removes every device; their push subscriptions go at the next
  push. There is no default. Both drop the open pairings, whose links
  name the old install.
- The old file is copied first to `<key file>.<UTC time>.bak` beside it,
  mode 0600, never over another backup. The new file then replaces the
  old one through a temp file and a rename, under the key file's lock.
  The backup holds the old token and keys: delete it once the new key
  works.
- The running bot takes the new identity before its next poll
  (`RelayClient.reload` compares the key file with the identity in use,
  so it comes at most one long-poll wait later). It polls under the new
  id, and every session and event stream of the old one ends. A restart
  of the bot does the same.
- A phone keeps the relay URL, the install id, the client key and the
  bot's key it took at pairing, and the app does not yet take new ones
  from the bot. So today every phone pairs again after a rotation, in
  either mode, and after `keep` the owner removes its old entry (Later
  work). `keep` is for the app that follows a rotation: its device then
  needs no new pairing and no new approval.

The command prints the old and new install ids, what it kept and
dropped, and the backup's path as JSON, and the owner's steps on stderr.
Moving to another relay is a change of `url` in the relay block and a
restart; a rotation first leaves the old relay a token that names an
install no bot uses.

## For the web front end (A)

```python
# WebFrontend.start(), once the HTTP server thread runs (_start_relay):
self.relay = start_relay(cfg, self.loopback_url(), logger=log, notify=self._relay_notice)
# WebFrontend.stop():
if relay is not None: relay.stop()
```

`cfg` is the web block with `pair_token_file` resolved and `key_file`
defaulted; A checks where the key file lives first (Config, above).
`_relay_notice(text)` posts the relay client's notices (a new device)
in the owner's DM channel through A's own `post`, which also pushes it. `loopback_url()` names loopback even when `bind` is a wildcard
(`0.0.0.0` reads as `127.0.0.1`, `::` as `::1`); a bind on any other
address leaves the relay off, since the client replays requests on
loopback only. Any failure is logged and the tailnet chat goes on.

`start_relay(frontend_cfg, local_base_url, *, logger=None, notify=None) -> RelayClient | None`
takes the `web` dict and the loopback base. A passes it only a `web` block
read from the owner-private `chat.json`, never one from `ea-config.json`,
which the resident can write: `relay_origin_access` and `url` are trust
settings. It returns None when there is
no `relay` block, it is disabled, `cryptography` is missing or the config
is wrong; it logs why, and the web chat keeps working on the tailnet.

A also adds owner-only routes for the owner's web chat, which the relay never
forwards (`/api/relay/` is denied on the bot side), and refuses them, and
`/api/pair`, with 403 when a request carries `X-Tacitus-Relay`:

| Route | Answers |
|---|---|
| `POST /api/relay/pair` | `{}` or `{"via": "own" or "relay"}` → `{"link", "qr", "expires", "origin", "bundle", "access"}`: `link` from `self.relay.new_pairing()` (version 2, with `b` when the bot has a bundle hash), `qr` the QR code's text (the same string), `expires` in Unix seconds, `origin` the `via`, `access` what the device may do once approved (`"all"` own, `"chat"` relay, which presses card buttons only with the owner's `cards`); own: 409 when the link would not open the bot's own origin (no `public_url`); relay: the link opens `<relay url>/`, 409 when the bot has no bundle hash; 400 for another `via` |
| `GET /api/relay/devices` | `self.relay.devices()` (`device_row`: id, name, paired_at, origin, pending, `code` while pending; for a relay-origin device `cards_allowed`, `bundle_verified` and, when verified, `release`) |
| `POST /api/relay/approve` | `{"device": <16 hex>, "cards"?: bool}` → `{"ok": true}`: `self.relay.approve(id, cards=...)`, the device's next request works, and a relay-origin device may press card buttons only with `cards` true (approving again changes it); 400 for a malformed id or `cards`, 404 for an unknown one |
| `POST /api/relay/revoke` | `{"device": <16 hex>}` → `{"ok": true}`: `self.relay.revoke(id)`; its sessions and its open event streams end at once (a stream with `err auth`), and its push subscriptions are dropped; 400 for a malformed id, 404 for an unknown one |
| `GET /api/relay/status` | `{"enabled": bool, ...self.relay.status()}` (install_id, relay_url, bot_public, client_key, sign_public, connected, last_ok, last_error, sessions, streams, devices, pending), and how "Pair a phone" can pair: `pair_own` (a link to the bot's own origin), `pair_relay` and `bundle` (the bundle hash signed now, or null); `{"enabled": false}` with no relay running |
| `POST /api/relay/attest` | `{"bundle": <64 hex>}`, only with `X-Tacitus-Relay` (400 otherwise): `self.relay.attest(device, hash, release)`; 200 `{"ok": true, "bundle_verified": true, "release"}` for the bundle the bot signs now, 409 `{"error", "bundle_verified": false}` for another (the record then says false), 404 for an unknown device. It grants nothing |

With no relay running the others answer 404. `RelayClient.level(device)`
is `"all"` for an own-origin device. For a relay-origin one it is
`BUNDLE_ACCESS` (`"chat"`) when it was paired against the bot's signed
bundle, attested or not, else `relay_origin_access` (`"off"` keeping it
out); either is `"all"` when `cards_allowed` (never at `"off"`).
`WebFrontend.bundle_hash()` is the bundle hash signed now; a front end
that is not serving (the owner's CLI) signs with the key file's key, so
it gets the serving bot's hash for the same files. A push subscription
records the relay device it came through (`X-Tacitus-Relay`), or the
device the app names when it subscribes on the tailnet; the pusher drops
a subscription whose device is no longer paired, so a device removed
with the CLI loses its subscriptions at the next push. `/api/me` gains
`relay` (the client runs) and `card_actions` (false for a request marked
`X-Tacitus-Relay-Cards: deny`, or relayed without `allow`): the app greys
out a card's buttons on such a phone.

One invariant binds A and C: a request marked `X-Tacitus-Relay-Cards:
deny` never confirms a card. The bot side already refuses its card
actions; the web front end and the shadow composite must also never
resolve its message text as a typed card code (the Zulip front end does
that for Zulip input only, and must not see web input that way). Without
it, a relay-origin device at `"chat"` could type `send K7Q2` and reach the
Send gate. Nothing resolves web text as a code today; the invariant keeps
it so.

For own-origin mode, A's `Content-Security-Policy` on its pages names the
relay in `connect-src`, whenever an enabled `relay` block names a URL
with a plain origin (a scheme, a host, a port) and a plain path: the
origin alone (`connect-src 'self' https://relay.example.net`), or with
the path as a prefix for a relay under a path of a shared host
(`connect-src 'self' https://host.example.net/relay/`). Anything else
adds nothing.

## For the web app (B)

- One transport interface, two implementations: `api(method, path, body)`
  answers a `Response`, and `events(since, sink)` opens the event stream.
  `DIRECT` is `fetch` on the base path and `EventSource`; the relay
  transport seals each call (`POST /r/{install}/c` with `X-Relay-Key`,
  `credentials: 'omit'`), opens the answer and builds the `Response`, and
  opens the stream with a ticket. At start, and on each reconnect, a
  browser that holds a pairing asks `GET <base>/api/me` (four seconds at
  most): a 200 keeps the direct transport, anything else takes the relay.
  Without a pairing nothing changes.
- WebCrypto has every primitive: `deriveBits` with `X25519` and with
  `HKDF`, and `AES-GCM` with a 128-bit tag. The byte layouts above, and
  `relay_crypto.py`, are the reference; the app reproduces
  `tests/fixtures/relay_vectors.json` byte for byte in headless Chromium
  (and in WebKit where Playwright has it).
  The device key pair is kept, with the relay, the install, the client
  key and the bot's public key, in IndexedDB (`tacitus-relay`); Web
  Storage holds none of it. The private half is kept as PKCS#8 bytes
  and imported non-extractable for use: WebKit (the iPhone's home-screen
  app) does not give an X25519 CryptoKey back from IndexedDB, so a key
  kept as a CryptoKey was lost at the next launch. Ephemeral keys are
  made non-extractable and never exported.
- On a link with `#tr2=` (or `#tr1=`), remove the fragment, then pair
  (Pairing, above), and show the pairing code until the phone's requests
  work. On the relay's copy, refuse a link without `b`, and stop on a
  full-page notice before making a key when `verifyBundle` is missing.
- Open an answer only as the answer to its own request (kind `res`, the
  request's frame id) and an event only of its own stream (kind `sse`,
  the `open` frame's id).
- On `err` `session`, `replay` or `bad`, say hello again and retry a GET
  once. A POST is not sent again: an `err` frame is not sealed, and the
  relay can forge one after the bot ran the request. The app says
  instead that the bot may have it, and to look before sending again. On
  `auth`, show "the bot did not accept this phone" and keep trying; never
  drop the keys over an `err` frame. A 403 marked `pending` shows the
  pairing code again. A phone whose `/api/me` says `card_actions: false`
  sees card buttons greyed out with "approve this in the web chat on your
  computer" (unless the owner allows card buttons for it there), and a
  403 from a card action says the same.
- A stored file comes through the transport as a blob, and a blob page
  has the app's origin. So only a png, jpeg, gif or webp keeps its type
  (an image shows from it); anything else, an SVG or an HTML file among
  them, is `application/octet-stream`, and every link to a blob downloads
  it, never opens it. An upload through the relay is at most 10 MB.
- A push subscription names this browser's relay device, if it has one.
- The service worker leaves the relay's routes (`/r/<install>/...`) and
  any path ending in `/healthz` to the browser, like the API.
- The relay's copy of the app (a page on the relay's origin) runs the
  chat only after its check of its code (docs/webchat.md, Code
  integrity): the manifest through `/api/bundle`, the check against the
  pairing record's `bundle` (the QR code's hash, first time) and
  `signKey` (after), the service worker's check of each file, then
  `POST /api/relay/attest` (Pairing, step 6). Without the hash, or with a
  check that cannot run or does not match, it stops on a full-page notice
  ("Tacitus stopped") and opens no stream. The card buttons stay greyed
  out until the owner allows them for the device. The bot's own copy
  checks on its own origin, after start-up, and does not attest.
- The bot's own copy, when it runs over the relay, takes a new release
  through it after start-up: the manifest through `/api/bundle`, then
  `GET /api/app/<file>` for each file its service worker lacks, each
  checked against the manifest by the page and again by the worker. Then
  it shows "Tacitus updated. Close the app and open it again." It
  attests nothing, and a mismatch keeps nothing and goes to `/api/diag`
  (docs/webchat.md, Code integrity). The relay's copy does not do this:
  its code comes from the relay.

## The paid tier and what stays AGPL

Everything in this repository stays AGPL-3.0: the relay server, the bot
side, the protocol and the web app. Anyone can run a relay of their own
(below), and a self-hosted relay needs no allow file.

The paid tier is the operation of the hosted relay: its domain and TLS,
its uptime and monitoring, abuse handling, capacity, and keeping its copy
of the web app current. It holds back no feature from the code. Price per
install; activation is one line in the allow file. No account exists in
the relay: payment lives in a separate billing system, which maps one
subscription to one install id (the id is not a secret). Under AGPL
section 13 the hosted relay offers its source to its users; running the
published code unchanged, and saying which version, satisfies that and
lets anyone check what the relay does. An organisation that wants to run
a changed relay without publishing the changes can ask for a commercial
license (CONTRIBUTING.md, "Contributor license").

## Running the relay

Sized for one maintainer: one small VM, one Python process, no database,
no state worth backing up. Thread per connection serves on the order of a
thousand online installs (one long-poll each, plus open phone streams);
past that, the same protocol moves to an asyncio server with no change to
bots or phones.

**The hosting rule.** A browser gives a paired phone's key to every page
of the origin that holds it (IndexedDB is per origin). So a relay serves
the app (`--static`, `relay/install.sh --with-app`) only on a hostname
of its own, with nothing else on it, and never on a vhost it shares with
other pages (a lab site, a dashboard). A relay under a path of such a
host runs without `--static`: it serves no app, and only own-origin
devices exist; `relay/install.sh` installs it so unless `--with-app` is
given. Keep the relay host off the tailnet, or keep it from the bot host
with the tailnet's ACL: the relay only ever needs the bot to dial out.

**The release rule.** A relay that serves the app (`--with-app`) serves
the release that its installs run: the copy it serves is the pinned
commit's `webui/`, and a relay-origin phone takes it only when it matches
its own bot's signed manifest (docs/webchat.md, Code integrity). The
relay serves no manifest: the phone gets it from the bot, through the
sealed channel. The auto-update channel keeps the relay host and the bot
on one release (docs/updates.md: the bot's updater and the relay's
`tacitus-relay-update.timer` follow the same channel). When they drift,
one updated and the other not yet, the phone refuses the relay's copy on
"Tacitus stopped" with the file that differs; it opens again once both
run the same release. A relay with installs on several releases cannot
serve them all one copy; until it serves one copy per release,
relay-origin phones work only for the installs on its release.

**The healthz rule.** `/healthz` gives its counts (online installs,
streams, queued bytes) only to a request from the relay host itself
without `X-Forwarded-For`: the counts tell when an install is online.
Monitor `http://127.0.0.1:8471/healthz` on the host. Through the proxy
the nginx block answers `/relay/healthz` with 404, and the relay itself
would answer `{"ok": true}` alone.

`relay/install.sh` does steps 2 to 4 and 6 on a host that has git and
Python 3.9 or newer, as root and idempotently. It installs a commit you
name (`--commit <40 hex>`; the clone is checked against it), never the
tip of a branch: the repository takes outside pull requests. `--tag
vX.Y.Z` is taken only when `git verify-tag` passes against the release
key (an SSH public key) in `/etc/tacitus-relay/release-key.pub`; releases
are not signed yet, so use `--commit` until they are. It writes the unit
itself, never from the clone. `--install-id <id>` adds an install to the
allow file. `relay/snippets/nginx.conf` is the proxy block for step 1,
under the path `/relay/` of an existing HTTPS vhost, so the relay URL is
`https://<host>/relay`; `relay/snippets/nginx-http.conf` holds the rate
zones and the key check it names, for nginx's `http` block.

Operator steps (the maintainer, for the hosted relay):

1. A VM with a public DNS name, and nginx (or Caddy) for TLS in front of
   `127.0.0.1:8471`, on the same host. With nginx: put
   `relay/snippets/nginx-http.conf` in the `http` block (most
   distributions include `/etc/nginx/conf.d/*.conf` there) and
   `relay/snippets/nginx.conf` in the HTTPS server block, then `nginx -t`
   and reload. The block takes `/relay/` with `^~`, streams without
   buffering, reads for 120 s (past the long-poll's wait), appends the
   client's address to `X-Forwarded-For` (the relay meters new installs
   per address), ignores the relay's `X-Accel-*` headers, hides its
   `Set-Cookie` and `Service-Worker-Allowed`, answers `/relay/healthz`
   with 404, refuses a bot poll without `Authorization` and a phone POST
   without `X-Relay-Key`, allows 16 MiB on the two frame routes and 8 KiB
   elsewhere, limits each address, and keeps no access log. Another
   server must do the same; Caddy streams with `flush_interval -1`.
2. As root, from a checkout of the release you mean to run:
   `relay/install.sh --commit <sha> --install-id <id>`. It makes the
   system user `tacitus-relay` (no login), clones that commit into
   `/opt/tacitus-relay/src`, checks it, and copies `relay_server.py` to
   `/opt/tacitus-relay/` (and `webui/` only with `--with-app`).
3. The script writes `/etc/systemd/system/tacitus-relay.service`:
   `python3 -I /opt/tacitus-relay/relay_server.py --bind 127.0.0.1 --port 8471 --allow-file /etc/tacitus-relay/installs.txt`
   (`--static /opt/tacitus-relay/webui` only with `--with-app`) as that
   user, `Restart=always`, its stderr (the access lines) to the journal.
   The unit reaches no address but localhost, holds no capability, sees
   a read-only system, private users and devices, runs under a system
   call filter, and is capped at 768 MiB, 2,200 tasks and 4,096 open
   files (`systemd-analyze security` scores it 0.9).
4. A monitor on `http://127.0.0.1:8471/healthz` on the host.
5. At each release, run the script again with the release's commit.
6. For each subscription, add its install id to the allow file
   (`--install-id`, or one per line in `/etc/tacitus-relay/installs.txt`);
   remove it when the subscription ends. The relay reloads the file on
   change.

## Owner steps

For an install using the hosted relay (or the owner's own). `<code>` is
the install's code tree (`/opt/ea-system` after hardening); run each
command as the owner. Tailscale is optional: these steps need none.

1. Create the key file:
   `python3 <code>/scripts/webchat-relay.py init --key-file ~/.ea-private/webchat-relay.json --relay-url https://<relay>`.
   It prints the install id and the `relay` block.
2. Hosted relay only: give the install id at sign-up, so the operator
   activates it. A relay of your own serves the app with
   `relay/install.sh --with-app`, on a hostname of its own, at the
   release your bot runs (Running the relay).
3. Add the `relay` block to the `web` block of `~/.ea-private/chat.json`
   (`url` and `install_id`; `key_file` may stay out when the file is
   `webchat-relay.json` in the private directory). `public_url` may stay
   out when you have no tailnet.
4. Restart the bot: `sudo systemctl restart ea-chat-bot`. The log says
   `relay: connected`, and `GET /api/relay/status` in the web chat says
   `"connected": true`.
5. Open the web chat on the bot's machine itself:
   `http://127.0.0.1:8470/chat/` (the web block's `port` and
   `base_path`). Paste the pairing token from
   `<private dir>/webchat-pair.token` when it asks (docs/webchat.md,
   Identity and auth).
6. Open the drawer, tap "Pair a phone", then "Pair through the relay",
   and scan the code with the phone's camera. The phone opens the app
   from the relay, pairs, and shows "This phone waits for approval" with
   a six-digit code; your DM gets a notice with the device's name and
   code. Under "Pair a phone" the new phone waits with its code: tap
   Approve only when the two codes match. If a device you do not know
   waits there, tap Remove. Leave "Allow card buttons on this phone" off
   (step 7). The phone then checks its code against the bot's and opens
   the chat: it reads and chats, and the list says "verified copy of"
   your bot's release. If it says "Code verification unavailable", or
   "Tacitus stopped" with a file name, the relay and the bot run
   different releases: update the one that is behind (docs/updates.md)
   and open the app again. Never send a pairing link through chat or
   mail: whoever opens it first gets the pairing. Add the app to the
   home screen; it works anywhere through the relay.
7. Card buttons (Send, Create) on that phone are off. Whoever runs the
   relay serves the phone's code, so a dishonest relay can read its chat
   and type as you, at pairing and at each update of the app on the
   phone; with card buttons on, it could also press Send. When you accept
   that for one phone, tick "Allow card buttons on this phone" beside it
   under "Pair a phone", or run
   `python3 <code>/scripts/webchat-relay.py approve --key-file ~/.ea-private/webchat-relay.json --device <id> --cards`
   (`show` lists the ids); untick it, or `approve` without `--cards`, to
   take them back. Without them, you press card buttons in the web chat
   on your computer.
8. With Tailscale you may pair on the tailnet instead: with `public_url`
   set, open "Pair a phone" on the web chat's tailnet address while the
   phone is on the tailnet and signed in. That link opens the app on the
   bot's own origin; that phone runs code from the bot, never from the
   relay, and works off the tailnet through the relay too.
   Without the UI,
   `python3 <code>/scripts/webchat-relay.py pair --key-file ~/.ea-private/webchat-relay.json --public-url https://<host>.ts.net/chat`
   (own origin) or `pair --key-file ... --relay-origin` (the relay's
   copy, with the bundle hash) draws the QR code on the terminal
   (`--print-link` prints the link); `show` lists each device's origin
   and, for a relay copy, whether its code is verified and its card
   choice, and `approve --device <id>` (with `--cards` for card buttons
   on a relay copy) lets it in.
9. A lost phone: Remove beside it under "Pair a phone", or
   `python3 <code>/scripts/webchat-relay.py revoke --key-file ~/.ea-private/webchat-relay.json --device <id>`
   (`show` lists the ids). Remove in the UI ends its sessions and its
   open event stream at once and drops its push subscriptions; with the
   CLI, the bot sees the change at the phone's next frame and drops the
   subscriptions at the next push. A subscription made before this
   change names no device: Revoke it under Notifications in the drawer,
   or replace the pairing token, which drops them all. The client key in
   the phone's links stays valid: it lets a holder spend the relay's
   limits for this install, not reach the bot. Step 10 retires it.
10. A pairing link that leaked, or the links of a removed phone:
   `python3 <code>/scripts/webchat-relay.py rotate --key-file ~/.ea-private/webchat-relay.json --devices drop`
   (Rotation). It prints the new install id: give it to the hosted
   relay's operator, set it as `install_id` in the relay block when the
   block pins one, and pair each phone again. The running bot takes the
   new key within a minute; a restart works too.

## Later work

- Kept-alive connections on the bot side; an asyncio relay past about a
  thousand installs.
- A version in `/api/me`, so a relay copy whose code does not match can
  name both releases, not only the file its check names.
- The app follows a rotation: on the tailnet it reads
  `/api/relay/status`, and when the bot's key is the one it holds, it
  takes the new relay URL, install id and client key in place. A device
  that `rotate --devices keep` kept then needs no new pairing. Until then
  `keep` spares only the bot's key and the device records.
- Signed release tags (the auto-update work), so the operator installs
  with `--tag`.
- A check of the app's code outside the page (a browser extension, in
  the Code Verify pattern), so a relay that serves changed code on
  purpose cannot pass the check; and a relay that serves one copy of the
  app per release.
