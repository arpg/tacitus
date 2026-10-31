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
| `scripts/webchat-relay.py` | The owner CLI: `init`, `show`, `pair`, `revoke` |
| `tests/test_relay.py` | Crypto, relay rules, the web front end's routes, and a loopback run of the whole path |
| `tests/fixtures/relay_vectors.json` | The protocol with fixed keys, made by `relay_crypto`; the app reproduces it in a browser (`tests/test_webui.py`) |

## What it is for

The owner reaches the web chat over the tailnet. A phone off the tailnet
(on cellular, without Tailscale, or on a managed device that cannot run
it) reaches it through the relay: a small public service that Tacitus
runs. The relay must never be able to read a message. An install's mail
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

**Own origin (recommended; the default when the web block has
`public_url`).** The owner pairs while the phone is on the tailnet. The
pairing link opens the app from the bot's own origin (`public_url`, the
tailnet address), and B's service worker caches the static files there.
Off the tailnet, the cached app starts from the service worker (a fetch
that has not answered in four seconds gives way to the cached copy) and
calls the relay cross-origin. The relay answers CORS on `/c` and `/sse` with no
credentials, since the client key header and the sealed frames are the
only authentication. The relay never delivers code to this device. A
compromised relay sees ciphertext and nothing else.

**Relay origin.** For a phone that never joins the tailnet, the relay
serves a copy of the app (`--static`). With an honest relay, the
guarantee is the same. A compromised relay can serve changed code to that
phone, read what it shows, and type as the owner. The bot therefore marks
a device paired through the relay's origin (`origin: "relay"`) and, by
default (`relay_origin_access: "chat"`), never lets it reach a card
action: the Send, Create and Publish buttons stay out of a compromised
relay's reach in both modes. `"read"` makes such a device read-only;
`"all"` lifts the limit and is the owner's explicit choice.

The bot records `"own"` only when the link's app URL has the origin of
the web block's `public_url`, compared normalized: the scheme, the host
lowercased and without a trailing dot, and the port written out
(`https://host:443` is `https://host`). Any other app URL, the relay's
origin under any spelling, or a bot with no `public_url`, makes a
`"relay"` device.

The origin is a property of the device key, not a claim the phone makes.
The pairing secret travels in the fragment of a link that opens one
origin, so only a page on that origin reads it, and the device's private
key then lives in that origin's storage. A relay-origin page cannot read
the storage of the own-origin app (same-origin policy).

## Pairing

1. On the tailnet web chat the owner opens "Pair a phone" (in the
   channel drawer, shown only when the app reaches the bot directly and
   `/api/me` says `relay`). A's route calls `RelayClient.new_pairing()`
   and B draws the returned string as a QR code on a canvas (`qr.js`),
   with the link as text beside it and the paired phones below, each with
   a Remove button. Without the UI, `scripts/webchat-relay.py pair`
   prints the same string.
2. The string is a link, `<app url>#tr1=<base64url JSON>`. The JSON holds
   `v` (1), `r` (the relay URL), `i` (the install id), `k` (the bot's
   X25519 public key), `c` (the client key), `p` (the pairing id, 16
   bytes as hex), `s` (the pairing secret, 32 bytes) and `x` (the expiry,
   Unix seconds, ten minutes ahead). A browser never sends a fragment to
   a server, so opening the link does not put the secret in any log.
3. The app reads the fragment, removes it from the address bar
   (`history.replaceState`, before anything else), and on the tailnet
   refuses a link whose install id is not the one `/api/relay/status`
   names. It makes its own X25519 key pair (WebCrypto, private key
   non-extractable), keeps it in IndexedDB before it sends the pairing
   hello through the relay (so a lost welcome leaves a key the bot knows),
   and asks before it replaces an earlier pairing.
4. The bot checks the hello, records the device (id: the first 8 bytes of
   SHA-256 of its public key, as hex; the name the phone gave, cleaned and
   cut to 40 characters; the origin), spends the pairing and answers with
   a welcome.

A pairing works once, expires in ten minutes, and at most eight are open
at a time. A phone whose welcome was lost says a plain hello next: its key
is already registered.

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
| A replayed hello does nothing | the clock check and the ephemeral key cache; a replay that gets through after a bot restart yields a session no one holds keys for |

A session lives in the bot's memory until it is idle for twelve hours or
the bot restarts (at most 32 are kept). The phone answers an `err` of
`session` with a new hello.

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
| `err` | bot or relay | no | `{"e": code}` only: `session` (say hello again), `auth` (pair again), `replay`, `bad`, `busy`, `offline`, `timeout` |
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
| `GET /healthz` | operator | | `{"ok", "installs", "streams", "queued_bytes"}` |
| `GET /...` | phone | | the static app from `--static`, with a strict CSP |

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
  `/api/pair` or `/api/relay/...`, compared decoded and lowercased (the
  local server decodes each segment before it routes, so
  `/api/%72elay/pair` is refused too): pairing the web chat and managing
  relay devices happen on the tailnet only. The web front end refuses
  those routes again for any request that carries `X-Tacitus-Relay`.
- From the phone's headers only `content-type`, `accept` and
  `last-event-id`. The bot adds the owner's credential itself (the
  `ea_web` cookie from `pair_token_file`, else `Tailscale-User-Login`),
  `X-Tacitus-Relay: <device id>`, so A can tell a relayed request, and
  `X-Tacitus-Relay-Cards: allow` or `deny`: whether this request may
  confirm a card.
- Back to the phone only `content-type` and `content-disposition`.
- Bodies up to 10 MiB each way (the chat's file cap), about 13.4 MiB as
  a frame.
- The local URL must be `http` on loopback; the relay URL must be `https`
  (plain `http` only on loopback, for tests).
- A device paired through the relay's origin gets `relay_origin_access`
  (above); one paired through the bot's own origin gets everything.

## What the relay stores and logs

In memory, per online install: the client key (derived from the token),
the last poll time, the queue of phone frames for the bot, the requests
waiting for an answer (by frame id), the open event streams, the stream
tickets not yet spent, a rate bucket and whether a phone has used it. A
route goes 60 s after the bot's last poll. Per source address, a bucket
that meters new routes (the address itself is never logged). Nothing goes
to disk. There are no accounts. A restart drops every route: bots
reconnect within seconds and phones say hello again.

The optional `--allow-file` lists the install ids the relay serves (the
hosted tier's activation list); it is reloaded when it changes.

One log line per request, with no frame content, no query string, no
header and no address:

```
2026-10-30T22:00:00Z POST c t7iapcqjg3nnr2a3nj6p3dmafl 200 in=812 out=1103 41ms
```

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
clients; the relay's own socket timeout is 30 s.

## What a compromised relay can and cannot do

It **cannot**:

- read a request, an answer, an event, a file, a device name or the
  clock in a hello;
- change a frame, move a sealed body to another request, kind or
  session, or replay one (the counter window and the hello cache refuse
  it);
- answer as the bot (it has no bot private key);
- pair a device of its own: the pairing secret travels only in a link's
  fragment. The exception is a link that opens the relay's own origin,
  where changed code could read it; a device paired that way is a
  relay-origin device (below), and `show` lists it;
- reach the bot's loopback server: only a paired device's sealed request
  gets there, and the relay holds no device key (outside the relay-origin
  case below);
- press a card's buttons (Send, Create, Publish) through any device, under
  the default policy, or confirm one by a typed code (the invariant in
  "For the web front end");
- read earlier sessions after a later key leak (forward secrecy).

It **can**:

- see which installs are online and when, the addresses of the bots and
  phones that connect (at the network level, though the relay process
  does not log them), and each frame's kind, size to 256 bytes and timing;
- drop, delay or reorder frames, close streams, refuse service;
- forge an `err` frame, which is not sealed. The app reads one as a hint
  about the transport only: it says hello again, retries or shows a note
  (for `auth`, "the bot did not accept this phone"), and never drops its
  keys over one;
- keep the bot's relay token and the client key. They let it act as this
  install's bot at a relay, which it already is; moving to another relay
  means a new key file (`init`), since the token is the install's
  identity there;
- on a phone paired through the relay's origin, serve changed code: read
  everything that phone shows and post as the owner, without the card
  actions unless the owner chose `"all"`. A phone paired through the own
  origin is out of its reach.

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
    "relay_origin_access": "chat",
    "wait": 25,
    "enabled": true
  }
}
```

| Key | Meaning |
|---|---|
| `url` | The relay's base URL. Required. `https`. |
| `key_file` | The key file, owner-private, mode 0600. Default: `webchat-relay.json` beside the web chat's store (the owner-private directory). Created on the first start when missing and `install_id` is not set. |
| `install_id` | Optional pin: the bot refuses to start the relay when the key file is another install's, or missing. |
| `app_url` | Where a pairing link opens the app. Default: the web block's `public_url` (own origin); without either, the relay's origin. A device is own-origin only when this is `public_url`'s origin. |
| `relay_origin_access` | What a device paired through the relay's origin may do: `"read"` (GET only), `"chat"` (all but card actions; the default), `"all"`. |
| `wait` | The long-poll's wait in seconds (default 25, at most 55; the relay caps it at 25). |
| `enabled` | `false` keeps the block but leaves the relay off. |

The key file (JSON, mode 0600, rewritten through a temp file and a rename
under an flock on `<key file>.lock`):

```json
{"v": 1, "bot_private": "<base64url, 32 bytes>", "relay_token": "<base64url, 32 bytes>",
 "relay_url": "https://relay.example.net",
 "devices": [{"id": "b15625d7ae6abe8d", "public": "<base64url>", "name": "phone",
              "paired_at": 1793421333, "origin": "own"}],
 "pairings": [{"id": "<32 hex>", "psk": "<base64url>", "expires": 1793421933, "origin": "own"}]}
```

The bot refuses a key file that group or others can read. The key file
and the `relay` block are the owner's: the resident (`ea-bot`) can read
neither, so it cannot pair a device, read a session or move the bot to
another relay. `ea_config.get_chat` keeps a `relay` block only from the
owner-private `chat.json`: one in `ea-config.json`, which the resident can
write, is dropped.

## For the web front end (A)

```python
# WebFrontend.start(), once the HTTP server thread runs (_start_relay):
self.relay = start_relay(cfg, self.loopback_url(), logger=log)
# WebFrontend.stop():
if relay is not None: relay.stop()
```

`cfg` is the web block with `pair_token_file` resolved and `key_file`
defaulted. `loopback_url()` names loopback even when `bind` is a wildcard
(`0.0.0.0` reads as `127.0.0.1`, `::` as `::1`); a bind on any other
address leaves the relay off, since the client replays requests on
loopback only. Any failure is logged and the tailnet chat goes on.

`start_relay(frontend_cfg, local_base_url, *, logger=None) -> RelayClient | None`
takes the `web` dict and the loopback base. A passes it only a `web` block
read from the owner-private `chat.json`, never one from `ea-config.json`,
which the resident can write: `relay_origin_access` and `url` are trust
settings. It returns None when there is
no `relay` block, it is disabled, `cryptography` is missing or the config
is wrong; it logs why, and the web chat keeps working on the tailnet.

A also adds owner-only routes for the tailnet UI, which the relay never
forwards (`/api/relay/` is denied on the bot side), and refuses them, and
`/api/pair`, with 403 when a request carries `X-Tacitus-Relay`:

| Route | Answers |
|---|---|
| `POST /api/relay/pair` | `{"link", "qr", "expires", "origin"}`: `link` from `self.relay.new_pairing()`, `qr` the QR code's text (the same string), `expires` in Unix seconds, `origin` `"own"` or `"relay"` |
| `GET /api/relay/devices` | `self.relay.devices()` (id, name, paired_at, origin) |
| `POST /api/relay/revoke` | `{"device": <16 hex>}` → `{"ok": true}`: `self.relay.revoke(id)`, its sessions end at once; 400 for a malformed id, 404 for an unknown one |
| `GET /api/relay/status` | `{"enabled": bool, ...self.relay.status()}`; `{"enabled": false}` with no relay running |

With no relay running the other three answer 404. `/api/me` gains
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
relay's origin in `connect-src` (`connect-src 'self' https://relay.example.net`),
whenever an enabled `relay` block names a URL with a plain origin (a
scheme, a host, a port; anything else adds nothing).

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
  `tests/fixtures/relay_vectors.json` byte for byte in headless Chromium.
  The device key pair is generated non-extractable and kept, with the
  relay, the install, the client key and the bot's public key, in
  IndexedDB (`tacitus-relay`); Web Storage holds none of it.
- On a link with `#tr1=`, remove the fragment, then pair (Pairing, above).
- On `err` `session`, `replay` or `bad`, say hello again and retry once;
  on `auth`, show "the bot did not accept this phone" and keep trying;
  never drop the keys over an `err` frame (it is not sealed). A phone
  whose `/api/me` says `card_actions: false` sees card buttons greyed out
  with "approve this on the tailnet", and a 403 from a card action says
  the same.
- A stored file comes through the transport: an image shows from a blob,
  and a link takes the blob's address on a tap. An upload through the
  relay is at most 10 MB.
- The service worker leaves the relay's routes (`/r/<install>/...`) and
  `/healthz` to the browser, like the API.

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

Operator steps (the maintainer, for the hosted relay):

1. A VM with a public DNS name, and Caddy (or nginx) for TLS in front of
   `127.0.0.1:8471`, on the same host. The proxy must not buffer event
   streams (Caddy: `flush_interval -1`; nginx: `proxy_buffering off`), its
   read timeout must exceed the long-poll's wait (60 s is enough), and it
   must append the client's address to `X-Forwarded-For` (Caddy does;
   nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`),
   which meters new installs per address. Its access log holds no key and
   no frame: a stream's URL carries only a one-time ticket.
2. A system user `tacitus-relay` with no login, and the files in
   `/opt/tacitus-relay/`: `relay_server.py` and `webui/` (a copy of
   `botcore/botcore/chat/webui/`).
3. A systemd unit that runs
   `python3 /opt/tacitus-relay/relay_server.py --port 8471 --static /opt/tacitus-relay/webui --allow-file /etc/tacitus-relay/installs.txt`
   as that user, with `Restart=always`; its stderr (the access lines)
   goes to the journal.
4. A monitor on `GET /healthz`.
5. At each release, copy `relay_server.py` and `webui/` again and restart.
6. For each subscription, add its install id to the allow file; remove it
   when the subscription ends. The relay reloads the file on change.

## Owner steps

For an install using the hosted relay (or the owner's own). `<code>` is
the install's code tree (`/opt/ea-system` after hardening); run each
command as the owner.

1. Create the key file:
   `python3 <code>/scripts/webchat-relay.py init --key-file ~/.ea-private/webchat-relay.json --relay-url https://<relay>`.
   It prints the install id and the `relay` block.
2. Hosted relay only: give the install id at sign-up, so the operator
   activates it.
3. Add the `relay` block to the `web` block of `~/.ea-private/chat.json`
   (`url` and `install_id`; `key_file` may stay out when the file is
   `webchat-relay.json` in the private directory). Keep `public_url` set
   in the web block: it is what makes a paired phone an own-origin one.
4. Restart the bot: `sudo systemctl restart ea-chat-bot`. The log says
   `relay: connected`, and `GET /api/relay/status` on the tailnet says
   `"connected": true`.
5. Pair the phone while it is on the tailnet: open the drawer of the web
   chat on a screen you control, tap "Pair a phone", and scan the code
   with the phone. Without the UI,
   `python3 <code>/scripts/webchat-relay.py pair --key-file ~/.ea-private/webchat-relay.json --public-url https://<host>.ts.net/chat`
   prints the same link. Open the link on the phone and add the app to
   the home screen. The app now works off the tailnet too.
6. A phone that never joins the tailnet: pair it with `pair` and no
   `--public-url` (the link opens the relay's copy of the app). It can
   read and chat; card buttons stay on the tailnet unless you set
   `relay_origin_access` to `"all"`.
7. A lost phone: Remove beside it under "Pair a phone", or
   `python3 <code>/scripts/webchat-relay.py revoke --key-file ~/.ea-private/webchat-relay.json --device <id>`
   (`show` lists the ids). Its sessions end at the next frame.

## Later work

- Kept-alive connections on the bot side; an asyncio relay past about a
  thousand installs.
- A version in `/api/me`, so a relay-origin app notices that it is older
  or newer than the bot.
- A `rotate` command for a new token and install id with the same paired
  devices, for moving between relays.
- Entries for these files in `docs/ARCHITECTURE.md`'s file index once the
  web chat lands.
