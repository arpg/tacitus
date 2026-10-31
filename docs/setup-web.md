# Setup in the browser

`scripts/install/setup_web.py` serves a first-run setup page, so a new
user answers the install questions on a phone or a laptop instead of in a
terminal. It asks the same questions as `scripts/install/user_init.py`,
checks the answers with the same code and writes the same files. The
root steps (the service account, the hardening, the services, the cron
lines) stay where they are; the page lists them at the end, word for
word, for the user to run.

```bash
python3 scripts/install/setup_web.py                       # this machine only
python3 scripts/install/setup_web.py --bind 100.101.102.103 # this machine's tailnet address
```

The script prints one address, with a token after `#t=`. Open it as
printed. From a laptop over SSH, `ssh -L 8480:127.0.0.1:8480 <host>`
and the default bind work as well. The page stops when you press
**Done**, after 60 minutes with no request (`--idle-minutes`), or on
Ctrl+C.

## One set of questions

`scripts/install/setup_questions.py` holds every question of every
phase as data: the answer key, the kind (`text`, `secret`, `yesno`,
`list`, `int`, `time`, `choice`), the label, the default sources, the
condition that shows it, and the notes the terminal prints before it.
Both front ends read it:

- `user_init.py` walks a phase's questions in order and asks each one at
  the terminal, or takes it from `--answers FILE`;
- `setup_web.py` sends the same list to the page (`GET /api/phases`) and
  resolves a posted phase with the same walk (`POST /api/answers`).

A posted phase is an answers file for that phase: the same keys
(docs/install-testing.md lists them), the same rules for an absent or
empty value, the same checks. The page's walk does not stop at the
first error: the page gets every error at once, keyed by answer, and
nothing is written until the phase is clean. Then the phase's writer in `user_init.py` (the one the
terminal calls) writes the files.

The page has a Previous button, so a phase posted again must not undo
a later one: the scheduling writer keeps the reply styles (the style
phase) and the VIP contacts the EA learned, and the autonomy writer
takes archive-as-completion from the inbox phase posted in the same
session, else from `autonomy-tiers.json`, as the terminal's `--phase
autonomy` does. The
identity writer keeps the institution's letterhead and boilerplate from
the file as it is, not from the copy without the template's values.

A default comes from a list of sources, the first that has a value: the
existing config file (the `--update` view: `GET /api/answers`), an
earlier answer of the phase (the institution name from the university,
the internal domain from the work address), the detected timezone, a
literal. A question that only the terminal can act on (`graph-sign-in-now`,
`google-calendar-now`, the "add another" loop questions, the writing-style
scan) is marked `terminal_only` and is not on the page.

## Screens

One phase per screen, Previous and Next, in the order of a full terminal
run: `profile` (the profile and the "Your responsibility" notice from
README.md, word for word), `identity`, `scheduling`, `inbox`, `autonomy`,
`groups`, `deadlines`, `style`, `chat` (the chat platform, the web chat
first, with this machine's tailnet name and the owner's Tailscale login;
only on `assistant`), `auth` (the app registration values, never a
sign-in), `reimbursement`, `vip`, `push`. A phase that asks this install
nothing is left out. Next posts the phase; an error shows under its
field. The profile and the chat platform decide what the later screens
ask, so the page reads the phases again after each. The last screen runs the `state` phase,
then the doctor (`scripts/ea-doctor.py --json`), and shows its checklist
as green, amber and red rows, with each fix, and the commands that are
left. The page is phone first, follows the system's dark or light theme,
and is three files: `setup_web/index.html`, `setup.js`, `setup.css`.

The `profile` phase is new and also on the terminal
(`user_init.py --phase profile`, keys `profile` and `acknowledged`). It
records `"profile"` and `"acknowledged"` in `ea-config.json` the way
`setup.sh --profile <name> --acknowledged` does, starting from the
template when the file does not exist yet, so `setup.sh` run later finds
both recorded and changes neither.

## What stays on the terminal

The page lists these at the end, with the commands:

- `./scripts/install/setup.sh --profile <name>`: the Python packages, the
  config templates the page did not write, the saved cron lines;
- the Graph and Google sign-ins (a browser redirect on this machine or a
  pasted address), and the writing-style scan, which reads sent mail;
- the service account, ea-bot's Claude login, the install overlay, the
  chat platform's own step on `assistant` (the `tailscale serve` line for
  the web chat, the Zulip bootstrap or the Slack app), the hardening and
  services, the cron install, the doctor, the first briefing, the
  first-week questions.

Also terminal only: `--chat zulip` and its server values (the chat
block), more than one group list or deadline domain per run, and
`--style-only`.

## API

Every route under `/api/` needs the token in the `X-Setup-Token` header.

| Route | Does |
|---|---|
| `GET /api/phases` | the web phases and their questions for this install's profile and chat platform, the notice, the finishing steps |
| `GET /api/answers` | the current value of every question: the existing config, else the default |
| `POST /api/answers` | `{"phase": id, "answers": {key: value}}`: 422 with `{"errors": {key: message}}`, or 200 with the files written |
| `POST /api/doctor` | runs the doctor; `{"results": [{status, name, detail, fix}], "pass", "warn", "fail"}` |
| `POST /api/done` | answers, then stops the server |

## Bounds and trust

The patterns are the web chat's (`botcore/botcore/chat/web_frontend.py`):

- It binds loopback by default. `--bind` takes a loopback address or a
  Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48) and nothing
  else: the page carries a client secret and chat tokens over plain HTTP,
  so it is served only where the network itself is private (WireGuard on
  the tailnet). `--host NAME` adds a name the browser may use (the
  MagicDNS name).
- A token of 32 random bytes per run, printed in the address after `#`:
  the fragment never reaches a request line, a log or a Referer. The page
  keeps it in `sessionStorage` and sends it as `X-Setup-Token`; it is
  compared in constant time. No identity header counts.
- Every request names this server in `Host` (421 otherwise); a request a
  page of another site made (`Sec-Fetch-Site`) is refused; a POST carries
  this page's exact `Origin` and a JSON body.
- A body of at most 64 KiB, answers of at most 2,000 characters and 50
  list items, one line each; a 30-second socket timeout; 16 connections
  at once; the doctor stops after 120 seconds.
- The pages load only their own files (`default-src 'none'`, scripts and
  styles from `'self'`, no inline code, nothing from another origin);
  every answer is `no-store`. The page draws text as text, never as HTML.
- A secret is never read back: `GET /api/answers` returns no secret, and
  the page never stores an answer. The Graph client secret and the Slack
  tokens go to the owner-private `~/.ea-private/secrets.env` (0600 in a
  0700 directory) and the push settings to `~/.ea-private/push.json`,
  by the same writer as the terminal; ea-bot never reads either
  (docs/SECURITY.md, `preprocessor/owner_trust.py`).
- It refuses to run as root or as `ea-bot`, and it writes `~/.ea` as the
  invoking user, so it belongs before the service account, as the
  `user_init.py` phases do. After `setup-ea-user.sh` the config belongs
  to ea-bot and a write fails with a message that says so.

## Tests

`tests/test_setup_web.py`: the phases JSON is the definitions; posted
answers write byte for byte what `user_init.py --answers --phase <p>`
writes from `tests/install/answers-email.json`; validation errors come
back keyed and write nothing; the token, Host, Origin and size gates;
the doctor route returns rows; Done stops the server; the bind rule; and,
where Playwright and Chromium are installed, the page in a phone-sized
browser from the profile screen to the checklist.
