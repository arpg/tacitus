# Published OAuth apps: sign-in with no registration

- **Status:** design and code, with the identifiers empty. The Tacitus
  project has not registered the two apps yet. While
  `preprocessor/published_apps.py` holds empty ids, every client, the
  setup questions and the setup page behave as before; the doctor adds
  an informational row that names the sign-in path. "Maintainer steps"
  below is the one-time work that fills them.
- **Code:** `preprocessor/published_apps.py` (the ids, the scopes per
  profile, the admin-consent link), `preprocessor/graph_client.py`
  (`_client_choice`, `_published_seed`, `_loopback_sign_in`),
  `preprocessor/google_calendar_client.py` and
  `preprocessor/gmail_client.py` (`_client_source`),
  `scripts/install/setup_questions.py` (`_auth_phase`),
  `scripts/install/user_init.py` (`published_graph_env`),
  `scripts/install/setup_web.py` (`next_steps`), `scripts/install/setup.sh`,
  `scripts/ea-doctor.py` (`graph_sign_in_path`, `google_client_path`),
  `tests/test_published_oauth.py`.
- **Companions:** [SETUP.md](SETUP.md) "Option D: no registration (the published app)", [it-request.md](it-request.md)
  "No registration: consent to the Tacitus app",
  [macos-native-accounts.md](macos-native-accounts.md) "The long-term path".

## In one paragraph

Today every install needs an app registration of its own: an Entra app
that the user's IT group registers (Options A and B), or a Google Cloud
project per user. The project instead registers once: one multitenant
Microsoft Entra application and one Google OAuth client of the desktop
type, both **public clients**. Each runs on the user's machine, holds no
confidential secret, and signs the user in with the authorization-code
flow and PKCE on a loopback redirect. A user at any institution signs in
with no registration. For Microsoft, each institution's administrator
consents once for the whole tenant. Tokens go from Microsoft or Google to
the user's machine and nowhere else: the project runs no server and
holds no token. Options A, B and C stay, and a client the user
configured always wins.

## What the project registers

### Microsoft: one multitenant Entra app

| Setting | Value | Why |
|---|---|---|
| Supported account types | Accounts in any organizational directory (multitenant) | Users of every Entra tenant can sign in once their tenant consents [M1] |
| Authority in the code | `https://login.microsoftonline.com/organizations` | Work and school accounts of any tenant; `common` would also admit personal accounts [M1], and getSchedule does not support those [M2] |
| Platform and redirect | Mobile and desktop applications, `http://localhost` | The value for apps that use the system browser [M3]. Entra ignores the port of a localhost redirect when it matches one, so the code listens on 8400 (or the port in `GRAPH_REDIRECT_URI`) [M4] |
| Second redirect | `https://login.microsoftonline.com/common/oauth2/nativeclient`, same platform | Where the admin-consent link lands: a blank Microsoft page, not a refused connection on the administrator's own machine |
| Allow public client flows | Yes | The device-code flow, for a machine with no browser, needs it [M3] |
| Client secrets and certificates | None | A public client cannot keep a secret [M5] |
| Web and single-page platforms | None | No web server can receive an authorization code |
| API permissions | Delegated only (the table below); no application permissions | The app acts only as the signed-in user |
| Publisher domain and verification | The project's verified domain and Partner One ID | The blue badge and the consent policies that require it ("Publisher verification") |

The sign-in (`graph_client._published_seed`): MSAL's
`initiate_auth_code_flow` makes the PKCE verifier and challenge
(`msal/oauth2cli/oauth2.py`), the code prints the link and opens a
browser, a listener on `localhost:8400` catches the redirect, and MSAL
redeems the code with the verifier and no secret [M5]. The listener binds
both `127.0.0.1` and `::1`, so no other local process holds the family
a browser may resolve `localhost` to; it answers any request other than
the redirect with the flow's `state` with a 400 and keeps waiting, and
gives each connection five seconds, so neither a stray request nor an
idle connection ends or stalls the sign-in. A `GRAPH_REDIRECT_URI` whose
port is not one ends the browser attempt with that error, and the
device code follows. When no browser
can reach the machine (Linux with no display, a Mac or WSL over SSH,
`GRAPH_AUTH_METHOD=device_code`), it prints a device code instead [M5];
a browser sign-in that sees no redirect within five minutes falls back
to the device code too. An error from Entra (no consent, a blocked flow)
ends the sign-in with the next step, since another flow meets the same
policy. Over SSH with device code blocked, `GRAPH_AUTH_METHOD=interactive`
(in `~/.ea-private/secrets.env`, "Coexistence") and `ssh -L
8400:localhost:8400` let a laptop's browser finish the loopback sign-in. The refresh token then renews silently, as on Options A
and B.

**Delegated permissions, the least per feature.** The code asks for the
set of the install's profile (`published_apps.GRAPH_SCOPES_BY_PROFILE`);
setup writes it to `GRAPH_SCOPES`. The registration lists the union, so
one tenant-wide consent covers every profile. The registered list must
contain everything the code requests, or a user is still blocked after
the administrator consents [M6].

| Feature | Graph call | Least privilege | Profiles |
|---|---|---|---|
| Read mail, threads, search | `/me/messages`, `/me/mailFolders` | `Mail.Read` | review-only (the others hold it through `Mail.ReadWrite`) |
| Drafts, replies, filing, categories, a draft's attachments | `POST`/`PATCH` `/me/messages`, `/move` | `Mail.ReadWrite` (sends nothing) [M7] | email, assistant |
| Send after the owner clicks Send | `/me/messages/{id}/send` | `Mail.Send` | assistant |
| The owner's calendar and an event's details | `/me/calendarView`, `/me/events/{id}` | `Calendars.Read` (`ReadBasic` omits the body) [M7] | review-only, email |
| Other people's free/busy | `/me/calendar/getSchedule` | `Calendars.ReadBasic` [M2] | every profile; the only one a native Mac needs |
| Create, move and delete events, EA holds | `POST`/`PATCH`/`DELETE` `/me/events` | `Calendars.ReadWrite` | assistant |
| Time zone and working hours | `/me/mailboxSettings` | `MailboxSettings.Read` | review-only, email, assistant |
| A refresh token; the account's name | (MSAL adds them) | `offline_access`, `openid`, `profile` | all |

| Profile key | Scopes requested |
|---|---|
| `availability` (a Mac with native mail and calendars) | `Calendars.ReadBasic` |
| `review-only` | `Mail.Read Calendars.Read MailboxSettings.Read` |
| `email` | `Mail.ReadWrite Calendars.Read MailboxSettings.Read` |
| `assistant` (and the default) | `Mail.ReadWrite Mail.Send Calendars.ReadWrite MailboxSettings.Read` |

Left out: `User.Read` (the ID token names the account), `Contacts.Read`,
`People.Read` and `Files.Read` (the code calls none of them), and
`Tasks.ReadWrite` (Microsoft To Do, optional; an install that wants it
uses its own registration). None of the requested permissions needs
admin consent by Graph's own table [M7]; the next section says why an
administrator consents anyway.

### What an institution's IT does once

Microsoft lets a user consent to these permissions, but a tenant can
turn user consent off, and Microsoft recommends allowing it only for
apps from verified publishers and for permissions the tenant classifies
as low impact [M8]. Tacitus needs mail and calendar access, so the
reliable path is one administrator consent per tenant:

1. The administrator opens the admin-consent link, which
   `python3 preprocessor/published_apps.py admin-consent-url` prints:
   `https://login.microsoftonline.com/organizations/v2.0/adminconsent?client_id=<id>&scope=<one profile's scopes>&redirect_uri=https://login.microsoftonline.com/common/oauth2/nativeclient&state=tacitus`.
   `organizations`, or the tenant's own id (`--tenant`), names the tenant
   [M9]. The prompt says "Accept for your organization" [M6]. Consent
   creates the app's service principal (an enterprise application) in
   the tenant and a tenant-wide grant [M1]. The link asks for the
   sign-in scopes and the `email` profile's scopes (drafts, filing,
   reading the calendar; no Send and no calendar writes) unless
   `--profile` names another: `review-only` for less, `assistant` for
   the Send and Create buttons, `all` for every scope the registration
   lists. A tenant grants the least its users need.
2. Or the administrator signs in to Tacitus once and ticks "Consent on
   behalf of your organization"; only a Privileged Role Administrator or
   above sees that box [M6].
3. Recommended: "Assignment required?" set to Yes on the enterprise
   application, with the users who run the EA assigned [M10]. Consent
   covers the whole tenant; assignment limits who can sign in, and so
   who a phishing link through the app can reach (the trust statement
   below). Also in the tenant: a Conditional Access policy can block the
   device-code flow, which Microsoft calls high risk and recommends
   blocking where possible [M11] (the browser sign-in still works); the
   sign-in logs show every sign-in by the app.
4. Revocation: Enterprise applications, the app, Permissions. After an
   administrator consents for the tenant, only an administrator can
   revoke, for the whole app [M1].

### What a user sees

- **Before the tenant consents:** the consent prompt names the app, the
  publisher ("Unverified" until the project is verified, then the
  publisher's name and a blue badge) and the permissions [M6]. Where the
  tenant allows no user consent, the user is told to ask an
  administrator, and can send a request from the prompt when the tenant
  runs the admin consent workflow [M6]. A new multitenant app from an
  unverified publisher also meets risk-based step-up consent: users
  cannot consent and see a warning that the publisher is unverified
  [M12]. The code turns the matching Entra errors (AADSTS65001, 90094,
  90095) into "send your IT group docs/it-request.md".
- **After the tenant consents:** the usual sign-in (account, password,
  multifactor, Conditional Access) and no consent prompt [M1]; then
  silent renewal. The doctor reports "the Tacitus app, no registration
  (Option D)".

### Publisher verification: steps and why

Why: the badge on the consent prompt; user consent policies that admit
verified publishers only [M8]; and without it, risk-based step-up
consent blocks users of other tenants and warns them that the publisher
is unverified [M12]. It certifies who publishes the app, not its quality
[M12].

What it needs [M12]: a Microsoft AI Cloud Partner Program (CPP, formerly
MPN) account that has completed verification, used through its partner
global account's Partner One ID (a location id does not work); the app
registered with a work or school account, in a tenant associated with
that partner global account; a publisher domain that is not
`*.onmicrosoft.com` and that matches the domain of the email used in the
CPP verification, or is a DNS-verified custom domain of the tenant; a
user who holds Application Administrator or Cloud Application
Administrator in Entra and CPP Partner Admin or Account Admin in Partner
Center, signed in with multifactor authentication; acceptance of the
Microsoft identity platform terms. There is no fee. National clouds are
not supported.

Steps [M13]: App registrations, the app, **Branding & properties**; set
the publisher domain; **Add Partner ID to verify publisher**; enter the
Partner One ID; **Verify and save**. Consent prompts show the badge
soon after, once the change replicates.

### Google: one desktop OAuth client

| Setting | Value |
|---|---|
| Client type | Desktop app (Google's `installed` client JSON) |
| Redirect | Loopback, `http://localhost:8400`; a desktop client takes any loopback port [G1] |
| PKCE | S256; `google-auth-oauthlib` makes the verifier (`Flow.authorization_url`) [G1] |
| Scopes the code requests | `calendar` (sensitive); `gmail.send` (sensitive); `gmail.readonly` and `gmail.compose` (restricted) [G7] |
| Audience | External, In production |

Google treats a desktop app as a public client that cannot keep secrets,
still issues it a client secret, and lists that secret as optional in
the token request [G1]. The Python libraries send whatever value
`PUBLISHED_GOOGLE_CLIENT_SECRET` holds; it is a public value like the
client id, not a credential. The manual copy-and-paste ("out of band")
redirect is gone [G1]; Tacitus uses the loopback redirect, and on a
machine with no browser the user copies the address the browser lands on
into the terminal, as the per-user path does today.

**Verification.** An app that requests sensitive or restricted scopes
and is not verified shows an unverified-app screen before the consent
screen and stops at 100 new users in total [G2]. Exempt: personal use
under 100 users, apps in testing, apps internal to one Workspace or
Cloud Identity organization, and apps an administrator marks trusted in
the Google Admin console [G3]. Verification needs brand verification, a
demonstration video, limits on data use and a justification for each
scope [G4]. A restricted scope adds an annual security assessment under
the App Defense Alliance's CASA framework, revalidated every year [G5],
and the Limited Use rules: data only for user-facing features, transfers
only with the user's consent for those features, and no human reading it
outside narrow exceptions [G6]. Workspace's own policy approves email
clients and generative AI summaries, and forbids using the data to train
any model beyond the user's own [G8]. Tacitus sends mail text to the user's own Claude account
to provide the user's features; the privacy policy must say so.

**Rollout order.** Calendar needs only the sensitive review, Gmail the
restricted one. The client ships once the Calendar review passes; Gmail
through it shows the unverified screen, within the 100-user cap, until
the security assessment passes. A Workspace institution can mark the app
trusted in the Admin console in the meantime [G3].

## Trust statement

- **The published apps hold no data and no secret.** The Entra app has
  no secret at all; the Google desktop client's secret is a public value
  by Google's own description [G1]. The project runs no server: tokens go
  from Microsoft or Google to the user's machine.
- **A client id is a name, not a credential.** A token needs the user's
  own sign-in (password, multifactor, the tenant's Conditional Access)
  and a consent. PKCE binds each authorization code to the process that
  asked for it [M5] [G1], and the loopback redirect never leaves the
  machine [M4].
- **What the id does allow.** Anyone can start a sign-in with a public
  client's id. Once a tenant consents, a phishing message that gets a
  user to finish a device-code sign-in hands the sender that user's
  tokens for the consented scopes; that is why Microsoft rates the
  device-code flow high risk [M11]. The tenant's answers are assignment
  [M10], a Conditional Access block on the device-code flow [M11], the
  sign-in logs and revocation [M1].
- **Tokens stay on the user's machine,** in the owner-private auth
  directory (`~/.ea/auth/msal_token_cache.json`, `google_token_*.json`),
  owned by the user and unreadable to `ea-bot`; the mail mediator uses
  them on the resident's behalf (SECURITY.md, Layer 5). There is no
  keychain integration today; the macOS native backend keeps no token at
  all ([macos-native-accounts.md](macos-native-accounts.md)).
- **What an owner of the registration can do.** A new permission needs
  a new consent from each tenant. But whoever owns the registration (the
  project's accounts, or anyone who takes one of them over) can add a
  redirect URI and a client secret without any tenant's consent, since
  neither is a permission. With them, a phishing link that gets a user
  of a consenting tenant to sign in sends that user's authorization code
  to a server the owner chose, and the user sees no new prompt for the
  scopes the tenant already granted. So the app is exactly as
  trustworthy as the accounts that own it, and the project cannot prove
  from here that it will not widen its reach this way. The owner can
  also disable the app, which stops every install that uses it: Options
  A, B and C remain the way out.
- **What the project does about it.** The registration lives in a
  dedicated tenant that holds nothing else; it has two owners, each with
  phishing-resistant multifactor authentication (a FIDO2 key), and no
  other account with rights over it; an alert on the tenant's audit log
  reaches both owners when anyone adds or changes a credential
  ("Certificates and secrets management") or a redirect URI ("Update
  application"); and the publisher is verified, so a look-alike app
  cannot borrow the badge (Maintainer steps).
- **What a tenant does about it.** Assignment required, with the users
  who run the EA assigned [M10], limits who the app can reach; consent
  to one profile's scopes limits what it can ask for; the sign-in logs
  and the enterprise application's audit history show new sign-ins and
  changes; revoking the consent stops it for the tenant [M1].
- **Nothing else moves.** The mediator, the buttons, the separate user
  and where data goes (it-request.md, "Where the data goes") are the
  same on every path.

## Coexistence

| Order | Microsoft (`graph_client._client_choice`) | Google (`_client_source`) |
|---|---|---|
| 1 | `GRAPH_CLIENT_ID` set: the user's registration, Option A or B as today | `~/.ea/auth/google_client_secret.json`: the user's own client |
| 2 | `PUBLISHED_GRAPH_CLIENT_ID` set: the Tacitus app (Option D) | `PUBLISHED_GOOGLE_CLIENT_ID` set: the Tacitus client |
| 3 | Neither: the earlier default (Microsoft Graph PowerShell's id); an owner plugin's token source (Option C) answers after the MSAL cache, as today | Neither: the error and the steps in SETUP.md "Google OAuth app" |

- A refresh token belongs to the client that got it. A cache that holds
  one from the earlier default, and none from the published app, keeps
  the earlier default, so an install never loses its silent sign-in to
  an upgrade. A Google token file names its own client, so old tokens
  keep refreshing through the client that issued them.
- The published app is never a confidential client: a
  `GRAPH_CLIENT_SECRET` left from Option A is ignored on that path.
- Setup offers the published app first, as choice 0 and the default;
  the registration methods keep their numbers 1 to 4, and an install
  whose `~/.ea/.env` records its own registration keeps that method as
  the default. The published choice writes `GRAPH_SCOPES` for the
  profile into the owner-private `~/.ea-private/secrets.env` and clears
  `GRAPH_CLIENT_ID`, `GRAPH_AUTH_METHOD`, `GRAPH_AUTHORITY` and
  `GRAPH_SCOPES` in `~/.ea/.env`; a tenant in `GRAPH_AUTHORITY` stays
  when the user sets one by hand.
- **Where the published path reads its settings.** `~/.ea/.env` is the
  resident's file, and `run-agent.sh` exports its `GRAPH_*` values into
  the owner's processes. For the published app an owner process takes
  `GRAPH_SCOPES`, `GRAPH_REDIRECT_URI` and `GRAPH_AUTH_METHOD` only from
  `~/.ea-private/secrets.env` (`owner_trust.PRIVATE_SETTING_KEYS`), so
  the resident cannot widen the scopes a sign-in asks for, move its
  redirect to a port it listens on, or pick its flow. Unset there, the
  published defaults apply: the assistant profile's scopes, port 8400,
  the flow by browser. The flows of a registered app (Options A and B)
  still read them from `~/.ea/.env`, as before.
- **The macOS native backend.** Mail and calendars stay native; the
  published app serves only other people's free/busy
  (`availability.provider` `graph`) with `GRAPH_SCOPES=Calendars.ReadBasic`
  in `~/.ea-private/secrets.env`,
  and the administrator can consent to that one permission with the
  link `published_apps.py admin-consent-url --profile availability`
  prints. This is the "long-term path" of
  [macos-native-accounts.md](macos-native-accounts.md).
- **Linux, macOS and containers** use the same two ids. A machine with
  no browser signs in with the device code (Microsoft) and the pasted
  address (Google).

## Maintainer steps (one time)

Microsoft, in a work or school tenant associated with the project's CPP
partner global account, with a DNS-verified custom domain:

1. Entra admin center, **App registrations**, **New registration**: name
   `Tacitus`; supported account types **Accounts in any organizational
   directory (Any Microsoft Entra ID tenant - Multitenant)**; redirect
   URI platform **Public client/native (mobile & desktop)**, value
   `http://localhost`. Register.
2. **Authentication:** under Mobile and desktop applications add
   `https://login.microsoftonline.com/common/oauth2/nativeclient`;
   **Allow public client flows: Yes**; add no Web or SPA platform.
3. **Certificates & secrets:** none.
4. **API permissions:** remove the default `User.Read`; add Microsoft
   Graph, delegated: `openid`, `profile`, `offline_access`, `Mail.Read`,
   `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadBasic`,
   `Calendars.Read`, `Calendars.ReadWrite`, `MailboxSettings.Read`
   (`published_apps.graph_registered_scopes()`). No application
   permissions.
5. **Branding & properties:** name, logo, home page (the repository),
   terms of service and privacy statement URLs, publisher domain (the
   verified custom domain); then **Add Partner ID to verify publisher**,
   **Verify and save**.
6. Test in a second tenant: the admin-consent link (if the `nativeclient`
   redirect is refused there, set `PUBLISHED_GRAPH_CONSENT_REDIRECT` to
   `http://localhost`; the browser then shows a refused connection after
   consent, which records it anyway [M9]); a browser sign-in; a device
   code; the badge on the prompt.
7. Commit the Application (client) ID as `PUBLISHED_GRAPH_CLIENT_ID` in
   `preprocessor/published_apps.py`.
8. Guard the registration (Trust statement): the tenant holds nothing
   but this app; **Owners** lists exactly two accounts, each with a
   FIDO2 key as its only sign-in method and no standing admin role
   beyond the app; an alert rule on the audit log (a Log Analytics
   workspace, or Microsoft Sentinel) mails both owners on
   `Update application – Certificates and secrets management` and on
   `Update application` events that change a redirect URI; the
   publisher stays verified (step 5).

Google, in a Cloud project the project owns:

1. **APIs & Services, Library:** enable the Google Calendar API and the
   Gmail API.
2. **Google Auth Platform, Branding:** app name, support email, logo,
   home page, privacy policy and terms (on a domain verified in Search
   Console, listed under authorized domains), developer contact.
3. **Audience:** External; **Publish app** (In production). Testing
   status ends refresh tokens after seven days (SETUP.md "Google OAuth
   app").
4. **Data access:** add exactly the scopes the code requests: `calendar`,
   `gmail.send`, `gmail.readonly`, `gmail.compose`. A request outside the
   list shows the unverified screen [G2].
5. **Clients, Create client, Desktop app** (`Tacitus desktop`); download
   the JSON.
6. **Verification Center:** submit for the sensitive scopes; then the
   restricted ones with the CASA assessment, renewed each year [G5].
7. Try a sign-in and a token refresh with `PUBLISHED_GOOGLE_CLIENT_SECRET`
   empty, since Google lists the secret as optional [G1]. If Google
   refuses, commit the secret too: `tests/secrets_lint.sh` then needs
   `preprocessor/published_apps.py` exempted from its Google
   client-secret pattern, and GitHub's secret scanning recognises Google
   OAuth client secrets,
   blocks the push under push protection and reports them to Google as a
   partner [G9]; bypass it as a public value.
8. Commit `PUBLISHED_GOOGLE_CLIENT_ID` (and the secret, if step 7 needs
   it).

After the commit, `python3 -m pytest -q tests/test_published_oauth.py`
covers both states, and the doctor of an install with no registration
reports the Tacitus app. What the project takes on: both registrations,
Google's yearly reassessment, a privacy policy, keeping the consent
screen's scopes equal to the code's, and administrators' consent
questions.

## Sources

- [M1] Convert a single-tenant app to multitenant (authorities, consent, service principal, revocation): https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant
- [M2] Microsoft Graph `calendar: getSchedule`, permissions (least privilege `Calendars.ReadBasic`; personal accounts not supported): https://learn.microsoft.com/en-us/graph/api/calendar-getschedule
- [M3] Configure desktop apps (redirect `http://localhost` for the system browser; "Allow public client flows" for the device-code flow): https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration
- [M4] Redirect URI restrictions, localhost exceptions (http allowed; the port ignored): https://learn.microsoft.com/en-us/entra/identity-platform/reply-url
- [M5] MSAL Python, acquiring tokens (public clients cannot store a secret; PKCE; `http://localhost`; device code): https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens
- [M6] Consent experience (the prompt, "Unverified", the admin's checkbox, admin approval, static permissions a superset of dynamic ones): https://learn.microsoft.com/en-us/entra/identity-platform/application-consent-experience
- [M7] Microsoft Graph permissions reference (descriptions; admin consent not required for the delegated Mail, Calendars and MailboxSettings permissions): https://learn.microsoft.com/en-us/graph/permissions-reference
- [M8] Configure how users consent to applications (the verified-publisher recommendation; built-in policies): https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent
- [M9] Admin consent protocols (the v2 `/adminconsent` endpoint; `organizations`; a registered redirect): https://learn.microsoft.com/en-us/entra/identity-platform/v2-admin-consent
- [M10] Restrict an app to a set of users ("Assignment required?"): https://learn.microsoft.com/en-us/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users
- [M11] Authentication flows in Conditional Access (device-code flow: high risk, block where possible): https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-authentication-flows
- [M12] Publisher verification overview (requirements, Partner One ID, the November 2020 step-up consent rule): https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview
- [M13] Mark an app as publisher verified (the steps): https://learn.microsoft.com/en-us/entra/identity-platform/mark-app-as-publisher-verified
- [G1] Google, OAuth 2.0 for iOS and desktop apps (public clients; client secret optional; loopback; PKCE S256; out-of-band removed): https://developers.google.com/identity/protocols/oauth2/native-app
- [G2] Google Cloud, unverified apps (the screen; 100 new users in total): https://support.google.com/cloud/answer/7454865
- [G3] Google Cloud, when verification is not needed: https://support.google.com/cloud/answer/13464323
- [G4] Google Cloud, verification requirements (sensitive and restricted scopes): https://support.google.com/cloud/answer/13464321
- [G5] Google Cloud, security assessment (annual, CASA): https://support.google.com/cloud/answer/13465431
- [G6] Google API Services User Data Policy (Limited Use): https://developers.google.com/terms/api-services-user-data-policy
- [G7] Google Cloud, restricted scopes (the Gmail list): https://support.google.com/cloud/answer/13464325
- [G8] Google Workspace API user data and developer policy (approved Gmail uses; AI and ML training): https://developers.google.com/workspace/workspace-api-user-data-developer-policy
- [G9] GitHub, supported secret scanning patterns (Google OAuth client id and secret: partner, push protection): https://docs.github.com/en/code-security/secret-scanning/introduction/supported-secret-scanning-patterns
