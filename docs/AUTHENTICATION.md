# Authentication Guide

This system connects to three external services: Microsoft Outlook
(Graph API), Slack, and Google Calendar/Gmail. Each has its own
authentication pathway with different lifetimes, refresh methods, and
failure modes.

---

## Quick Reference

| Service | Method | Token Lifetime | Refresh | User Action Needed |
|---|---|---|---|---|
| **Outlook** (confidential) | Auth-code + client secret | ~1h access / long-lived refresh | Auto (silent refresh) | Re-auth on password/secret change |
| **Outlook** (MSAL public) | OAuth2 device-code/interactive | Months (refresh token) | Auto (silent refresh) | Re-auth on password change |
| **Outlook** (owner plugin) | A token an owner-zone plugin's auth source returns | Set by the source | The plugin's own | See the plugin's README |
| **Outlook** (published app) | The Tacitus app: auth-code with PKCE, or device code | Months (refresh token) | Auto (silent refresh) | IT consents once per tenant ([published-oauth-apps.md](published-oauth-apps.md)) |
| **Slack** | Bot + App tokens | Indefinite | None needed | Only if app is reinstalled |
| **Google Calendar** | OAuth2 desktop app | Indefinite (refresh token) | Auto | Re-auth if revoked |
| **Google (MCP)** | Claude Code OAuth | Indefinite (refresh token) | Auto (Claude manages) | Re-auth on first use |


---

## 1. Microsoft Outlook (Graph API)

This is the most complex auth pathway, because what works depends on
the Microsoft 365 tenant's policy on app registrations. Two methods exist
in the core; a third comes from an owner-side plugin.

> **Which one applies to you.** Ask IT for an app registration with
> [it-request.md](it-request.md). If the registration comes with a client
> secret and "Allow public client flows" off, use **Option A**. If the
> tenant allows public clients (the "Mobile and desktop applications"
> platform with public client flows on), **Option B** is simpler. If the
> tenant registers no third-party apps at all, an owner-side plugin can
> supply the token from another source; see [plugins.md](plugins.md).

### Option A: Confidential Client (auth-code + secret)

**When:** the app registration has a client secret and "Allow public
client flows" is off. Device-code and public-client interactive sign-in
then fail with `AADSTS7000218` ("client_secret required"). The app is a
*confidential client*: you sign in through the browser once and the token
exchange uses the **client secret**. Many institutions provision only this
kind of registration, on a limited basis and with admin consent.

**What IT provides:**
- Application (client) ID and Directory (tenant) ID
- A client secret value (sensitive; treat like a password; it expires)
- Delegated permissions on the app (Mail.ReadWrite, Mail.Send,
  Calendars.ReadWrite, MailboxSettings.Read, User.Read, offline_access, …)
- A redirect URI registered under the app's **Web** platform, matching
  `GRAPH_REDIRECT_URI` (default `http://localhost:8400/callback`, the
  value `graph_client.py` listens on; it must be a loopback URL)
- Admin consent granted for the app (so no per-user approval prompt)

**Setup:**
```bash
# ~/.ea/.env  (non-secret)
GRAPH_AUTH_METHOD=confidential
GRAPH_CLIENT_ID=<application-client-id>
GRAPH_AUTHORITY=https://login.microsoftonline.com/<tenant-id>
GRAPH_REDIRECT_URI=http://localhost:8400/callback

# ~/.ea-private/secrets.env  (chmod 600, sensitive)
GRAPH_CLIENT_SECRET=<client-secret-value>

# Sign in once (opens a browser, catches the redirect locally):
python3 preprocessor/graph_client.py
```
`python3 scripts/install/user_init.py --phase auth` walks through all of this and writes the
files for you; choose method **confidential**.

**Ongoing operation:**
- One interactive sign-in seeds a refresh token into
  `~/.ea/auth/msal_token_cache.json`.
- Subsequent runs refresh silently via the cached refresh token + secret:
  no browser, headless-friendly. **No token-harvest cron needed.**
- Confidential-client refresh tokens are long-lived (until password change,
  admin revocation, or secret expiry).

**What expires:**
| Component | Lifetime | Recovery |
|---|---|---|
| Access token | ~1 hour | Auto (silent refresh) |
| Refresh token | Long-lived | Re-run `graph_client.py` after password change/revoke |
| Client secret | Set by IT (e.g., 6 to 24 mo) | Ask IT for a new secret (`AADSTS7000222` when expired) |

**Scope alignment:** every requested scope must be granted on the app
registration, or sign-in fails with a consent error. Setup handles this
automatically: it signs in requesting `.default`, reads the app's granted
delegated scopes, and writes an aligned `GRAPH_SCOPES` to `~/.ea/.env`, keeping
each granted scope, dropping a redundant `Mail.Read` when `Mail.ReadWrite` is
granted, and downgrading (e.g.) `Calendars.ReadWrite` → `Calendars.Read` when
only Read is granted. The full `SCOPES` list in `graph_client.py` stays the
default superset; `GRAPH_SCOPES` only narrows it.

When your admin later adds a permission (e.g., `Calendars.ReadWrite`), re-align
in one command:

```bash
python3 preprocessor/graph_client.py --align          # preview the new GRAPH_SCOPES line
python3 preprocessor/graph_client.py --align --write  # and write it into ~/.ea/.env
```

Do **not** just delete `GRAPH_SCOPES` to "get everything back": that requests
the full superset again, including scopes the app still lacks, and sign-in fails.
Re-aligning only adds what is actually granted.

**Files:**
- Token cache: `~/.ea/auth/msal_token_cache.json`
- Auth logic: `preprocessor/graph_client.py` (`_confidential_auth_code_flow`)

**Mail mediation:** in the hardened deployment (mail mediation on, the default;
see [SECURITY.md](SECURITY.md) Layer 5), the client secret and the token cache
are owned by you, not the `ea-bot` user that runs the LLM. The owner-side
`ea-mail-mediator` service loads `~/.ea-private/secrets.env` and runs `graph_client` on
the agent's behalf, so the confidential flow works end-to-end without the secret
ever entering the sandboxed subprocess. `run-agent.sh` and the Slack bot pass
`GRAPH_CLIENT_SECRET` to the agent only when mediation is disabled.

### Option B: MSAL public client (interactive or device code)

**When:** the tenant allows a public-client app registration: the
"Mobile and desktop applications" platform with "Allow public client
flows" on. No client secret exists, so nothing can leak from the machine
but the token cache.

**What it does:** Standard OAuth2 flow using Microsoft's MSAL library.
Opens a browser for consent (or prints a device code to enter on another
device when there is no browser), then stores a long-lived refresh token
that silently renews access tokens on every API call.

**Registration:**
1. portal.azure.com > App registrations > New registration
2. Name: "EA Assistant"; platform **Mobile and desktop applications** with
   redirect URI `http://localhost` (MSAL picks the port at sign-in; the
   device-code flow needs no redirect)
3. Authentication > Allow public client flows: **Yes**
4. Add delegated permissions: Mail.Read, Mail.ReadWrite, Mail.Send,
   Calendars.ReadWrite, Contacts.Read, MailboxSettings.Read

**Setup:**
```bash
# ~/.ea/.env
GRAPH_CLIENT_ID=<application-client-id>
GRAPH_AUTHORITY=https://login.microsoftonline.com/<tenant-id>
GRAPH_AUTH_METHOD=interactive      # or device_code over SSH, with no display

python3 preprocessor/graph_client.py
```

**Scopes:** without `GRAPH_SCOPES` the public-client sign-in requests the
full `SCOPES` superset in `graph_client.py` (mail, calendar, contacts,
mailbox settings, tasks, people, files), and every one of them must be
granted on the app, or the sign-in ends in a consent error. Set
`GRAPH_SCOPES` in `~/.ea/.env` to the scopes the app grants (space
separated, short names such as `Mail.ReadWrite Calendars.Read`) and the
sign-in asks for those only. `--align` serves the confidential method
only; for a public client, write the line by hand.

**Ongoing operation:**
- Refresh token lasts months (until password change or admin revocation)
- Access tokens auto-renew silently on every API call
- No cron needed, no manual intervention

**Files:**
- Token cache: `~/.ea/auth/msal_token_cache.json`
- Auth logic: `preprocessor/graph_client.py` (`_interactive_seed`)

### When the tenant blocks public clients or app registrations

Some tenants prohibit third-party app registrations, or register them
only without public client flows. The symptoms are the same everywhere:

- A registration with public client flows off fails device-code and
  interactive sign-in with `AADSTS7000218`. Use **Option A** with the
  secret IT issued.
- Microsoft's own well-known client IDs (Graph Explorer, Azure CLI, Office
  Desktop, Graph PowerShell and the like) fail with `AADSTS1001010` (no
  service principal in the tenant) or `AADSTS65002` (Microsoft blocks its
  first-party clients from third-party code). Do not run
  `graph_client.py` interactively with one of them; it fails and wastes
  time.
- A tenant that registers nothing leaves one path: an owner-side plugin
  that supplies the token from another source ([plugins.md](plugins.md)).

### Choosing Between Options

| Situation | Use |
|---|---|
| IT-provisioned app with a secret, public client flows off | **Option A** (confidential), no cron |
| Tenant that allows a public-client app registration | **Option B** (MSAL interactive or device code), simpler |
| Tenant that blocks app registrations entirely | An owner plugin's token source, such as [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth) |
| Tenant whose IT consents to the project's published app | **Option D** (SETUP.md), no registration; the doctor names it ([published-oauth-apps.md](published-oauth-apps.md)) |
| Headless server with no display (not even xvfb) | Option A or B with one sign-in from a desktop, or an owner plugin's token source |

The auth method is selected via `GRAPH_AUTH_METHOD` in `~/.ea/.env`
(`confidential` | `device_code` | `interactive`); if unset, a client secret
implies `confidential`, otherwise the public-client path is used. With no
`GRAPH_CLIENT_ID` and the project's published app registered, the
published app (always a public client) signs in instead. The bot's
`authenticate()` function tries MSAL silent auth (cached refresh token)
first, then the auth sources of enabled owner-zone plugins, then the
configured interactive seeding method.

### Token Sources From Owner Plugins

An owner-zone plugin can declare `owner_hooks.auth_sources`
([plugins.md](plugins.md)): functions that take no arguments and return
a token or None. `graph_client.authenticate()` asks them, in plugin then
manifest order, after the MSAL cache and before any interactive sign-in.
Only an owner process loads them, from the owner-private plugin
directory, after the owner and integrity checks. A token with an `exp`
claim is kept in the process until a minute before it expires.

The published one is [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth): a pasted token, or a
headless browser harvest every 30 minutes that reads the token Outlook on
the web holds for your own session. Use it only where your organization's
rules allow it. Its README has the install steps. To write your own,
start from [tacitus-plugin-template](https://github.com/arpg/tacitus-plugin-template).

**Upgrading from the pre-release line.** Before v0.1.0 the core carried
this path itself (`GRAPH_AUTH_METHOD=playwright`, a pasted
`manual_token.txt`, and an owner cron line running
`preprocessor/token_refresh.py`). v0.1.0 removed it, so such an install
loses Outlook access when its last token expires unless it installs
outlook-web-auth (or moves to Option A or B) when it upgrades. The
plugin's README, "Moving from the core's copy", lists the steps;
`scripts/ea-doctor.py` fails `auth: outlook web token path` until they
are done.

### REST API vs Graph API Compatibility

The system auto-detects your token's audience and routes API calls
accordingly:

| Token audience | API used | How to get |
|---|---|---|
| `https://graph.microsoft.com` | Microsoft Graph API v1.0 | MSAL app (Option A/B), or an owner plugin's token source |
| `https://outlook.office.com` | Outlook REST API v2.0 | An owner plugin's token source (Outlook on the web issues them) |

The EA system supports both APIs transparently. `graph_client.py`
reads the JWT audience claim of a plugin source's token and routes
requests to the correct endpoint:

- Graph: `https://graph.microsoft.com/v1.0/me/messages`
- REST: `https://outlook.office.com/api/v2.0/me/messages`

Response keys are normalized to camelCase regardless of which API
responds (the REST API uses PascalCase, Graph uses camelCase).

**Limitations of the REST API path:**
- No `$filter` on `flag/flagStatus` (flagged message detection
  returns empty; the daily briefing skips this check automatically)
- Calendar operations may have minor behavioral differences
- Microsoft is deprecating the REST API (no official sunset date yet
  but new features only ship to Graph)

---

## 2. Slack

Slack auth is the simplest: two tokens that never expire.

### Bot Token (`xoxb-...`)

**What it does:** Authenticates the bot to read channels, post messages,
upload files, and handle interactive buttons.

**How to get it:**
1. Create a Slack app at api.slack.com/apps (or use a shared one)
2. OAuth & Permissions > add scopes: `channels:history`, `channels:read`,
   `chat:write`, `files:read`, `groups:history`, `groups:read`, `users:read`
3. Install to workspace > copy Bot Token

**Lifetime:** Indefinite. Only expires if the app is uninstalled from
the workspace or an admin revokes it.

**Shared across users:** Yes. One Slack app serves all EA users in a
workspace. Each user gets their own private channel.

### App-Level Token (`xapp-...`)

**What it does:** Authenticates Socket Mode, the persistent WebSocket
connection that delivers real-time events (messages, button clicks)
to the bot without needing a public URL.

**How to get it:**
1. Slack app > Basic Information > App-Level Tokens > Generate
2. Add scope: `connections:write`
3. Copy the `xapp-...` token

**Lifetime:** Indefinite. Same as the bot token.

**Per-user:** Each user generates their own app-level token. This is
the only per-user Slack credential.

### Storage

Both tokens go in `~/.ea/.env`:
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
EA_SLACK_CHANNEL_ID=C0A...
EA_SLACK_CHANNEL_NAME=ai-assistant-yourname
```

### Failure Mode

If the bot token is revoked, the bot process exits immediately with a
Slack API error. Re-install the app and update `~/.ea/.env`.

---

## 3. Google Calendar and Gmail

Two independent auth pathways connect to Google services.

### Direct Python Client (Calendar)

**What it does:** The scheduling engine reads Google Calendar events
directly via the Calendar API to merge availability across Outlook
and Google calendars.

**Setup (per Google account):**
1. Create a Google Cloud project with Calendar API enabled
2. Create OAuth Desktop App credentials
3. Download client secret JSON to `~/.ea/auth/google_client_secret.json`
4. Run: `python3 preprocessor/google_calendar_client.py --setup`
5. Complete browser-based OAuth consent
6. Repeat for each Google Calendar account

**Token lifetime:**
- Access token: ~1 hour (auto-refreshed transparently)
- Refresh token: indefinite (until manually revoked or password change)

**Storage:** `~/.ea/auth/google_token_<email>.json` (one per account)

**Failure mode:** If the refresh token is revoked (rare), re-run
`--setup` for that account. The bot logs a warning but continues
operating with Outlook-only calendar data.

### MCP Connectors (Gmail + Google Calendar)

**What it does:** the claude.ai account connectors give the Claude
subprocess Gmail reads and a second Google Calendar pathway (tool names
`mcp__claude_ai_Gmail__*` and `mcp__claude_ai_Google_Calendar__*`; the
allowlists deny their write verbs).

**Setup:** they are not added with `claude mcp add`. Enable Gmail and
Google Calendar in claude.ai Settings > Connectors for the account ea-bot
logs in with, and finish each one's Google sign-in there; Claude Code
loads them from that account. `claude mcp list` as ea-bot shows them
(SETUP.md "Claude Code connectors").

**Token lifetime:** Indefinite refresh tokens, managed entirely by
Claude Code. No user maintenance required.

**Storage:** `~/.claude/.credentials.json` (Claude Code managed)

**Failure mode:** If Google OAuth is revoked, the next Claude run that
tries to use Gmail/Calendar will prompt for re-authentication.

---

## Technical Details

### Why the Outlook Auth Paths Exist

Microsoft's Graph API is the only way to get structured email access
(JSON responses, server-side search, draft creation, calendar events,
conversation threading). IMAP provides raw message access but lacks
calendar integration, draft management, and efficient search.

Graph access needs an app registration in the user's own Microsoft 365
tenant, and tenants differ in what they allow. Three outcomes cover them:

| Tenant policy | Path |
|---|---|
| Registers a confidential client (secret, public client flows off) | Option A |
| Registers a public client (public client flows on) | Option B |
| Registers nothing for third parties | An owner plugin's token source, such as [outlook-web-auth](https://github.com/arpg/tacitus-plugin-outlook-web-auth) |

Every well-known Microsoft client ID fails in a tenant that has not
consented to it, so none of them is a way around a registration:

| Client ID | App | Error |
|---|---|---|
| `de8bc8b5-...` | Graph Explorer | AADSTS1001010: no service principal |
| `9e5f94bc-...` | Thunderbird | AADSTS1001010 + only IMAP scopes |
| `1950a258-...` | Azure PowerShell | AADSTS65002: preauthorization required |
| `04b07795-...` | Azure CLI | AADSTS65002: Azure mgmt only |
| `d3590ed6-...` | Office Desktop | AADSTS65002: anti-impersonation block |
| `14d82eec-...` | Graph PowerShell | AADSTS1001010: blocked by tenant |

The AADSTS65002 error means Microsoft prevents its own first-party
client IDs from being used by third-party code. The AADSTS1001010
error means the app's service principal doesn't exist in the tenant.
Some tenants allow self-service registration in the Azure Portal
(portal.azure.com > App registrations); in others IT registers the app,
and [it-request.md](it-request.md) is the request to send.

### Token Flow: MSAL (When Available)

```
     ┌──────────────┐
     │ Any API call  │
     └──────┬───────┘
            │
     ┌──────▼───────┐     ┌───────────────────┐
     │ authenticate()├────►│ Try silent refresh │
     └──────┬───────┘     │ (cached refresh    │
            │             │  token, no browser) │
            │             └─────────┬───────────┘
            │                       │
            │              ┌────────▼────────┐
            │              │ Success? Return  │
            │              │ access token     │──► API call proceeds
            │              └────────┬────────┘
            │                       │ (fail)
            │              ┌────────▼────────┐
            │              │ Interactive auth │
            │              │ (opens browser)  │
            │              └─────────────────┘
```

### How Each Service Connects

```
┌─────────────────────────────────────────────────────┐
│                    EA System                         │
│                                                     │
│  ┌──────────────┐   ┌───────────────────────────┐  │
│  │ ea-chat-bot  │   │ Claude subprocess (ea-bot) │  │
│  │ (owner user)  │   │ (ea-bot user, sandboxed)   │  │
│  └──────┬───────┘   └──────────┬────────────────┘  │
│         │                      │                    │
│         │ Socket Mode          │ MCP connectors     │
│         │ (xapp- token)        │ (Claude OAuth)     │
│         │                      │                    │
│  ┌──────▼───────┐   ┌─────────▼──────────┐        │
│  │    Slack      │   │  Gmail MCP / GCal  │        │
│  │  (xoxb- bot   │   │  MCP servers       │        │
│  │   token)      │   │  (Google OAuth)    │        │
│  └──────────────┘   └────────────────────┘        │
│                                                     │
│  ┌──────────────────────────────────────────┐      │
│  │ preprocessor/ (Python, zero Claude cost)  │      │
│  │                                           │      │
│  │  graph_client.py ──► Graph / Outlook REST  │      │
│  │    (MSAL cache, then owner plugin tokens) │      │
│  │                                           │      │
│  │  google_calendar_client.py ──► GCal API   │      │
│  │    (google_token_*.json, auto-refresh)    │      │
│  └──────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

### Confirmation Tokens (Internal Security)

Certain actions (sending email, creating ANY event, rescheduling,
deleting events, scheduling a deferred send) are gated by one-time
confirmation tokens. These are not auth tokens for external services;
they're internal security controls that prevent the Claude subprocess
from taking high-impact actions without explicit user approval via Slack
buttons.

- Generated by `ea-chat-bot.py` when posting confirmation buttons
- Stored in `~/.ea/state/pending-confirmations.json`
- Expire after 30 minutes
- Consumed on use (one-time)
- Validated at CLI level (`graph_actions.py`), which then mints a
  process-local approval (`graph_client.issue_inprocess_approval`) for
  the library call. The library approval is a random value that exists
  only in that process's memory, so no file on disk can satisfy the
  second gate. See SECURITY.md Layer 3 for the one known exception.

A deferred send needs a SECOND credential because the 30-minute token is
long expired by the time it fires: `schedule-send` signs the queue entry
with an HMAC keyed by `~/.ea/auth/scheduled-send-hmac.key` (owner-only
`0600`), and the `*/5` cron verifies that signature before touching the
Graph token.

### File Reference

| File | Purpose |
|---|---|
| `~/.ea/auth/msal_token_cache.json` | MSAL refresh + access token cache |
| `~/.ea-private/plugins/<name>/` | Owner-zone plugins, such as a Graph token source ([plugins.md](plugins.md)) |
| `~/.ea/auth/google_client_secret.json` | Google OAuth app credentials |
| `~/.ea/auth/google_token_*.json` | Per-account Google refresh tokens |
| `~/.ea/auth/scheduled-send-hmac.key` | owner-only `0600` signing key for deferred sends (see SECURITY.md Layer 3) |
| `~/.ea/.env` | Slack tokens, channel config, env overrides |
| `~/.claude/.credentials.json` | Claude Code MCP connector credentials |
| `~/.ea/state/pending-confirmations.json` | One-time confirmation tokens |
| `preprocessor/graph_client.py` | Graph API auth logic (MSAL silent, then owner plugin auth sources, then interactive seed) |
| `preprocessor/google_calendar_client.py` | Google Calendar OAuth client |
