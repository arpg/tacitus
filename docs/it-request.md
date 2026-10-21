# Request to IT: an app registration for a personal email assistant

Send this page to your IT group. It asks for one Microsoft Entra app
registration so that your executive assistant (EA) can read your mail,
write drafts and check your calendar. It says what the app does, where the
data goes, and what stops the EA from acting on its own.

Fill in the items in angle brackets before you send it.

## The request

I run a self-hosted personal assistant on `<the machine: my workstation,
or a department server>`. It needs delegated access to my own mailbox and
calendar through Microsoft Graph. Please register an app with these
settings. The first column is a confidential client, which most policies
allow; the second is a public client, which needs no secret, if your
policy permits one.

| Setting | Confidential client | Public client |
|---|---|---|
| Account types | This organization only (single tenant) | Same |
| Platform | Web | Mobile and desktop applications |
| Redirect URI | `http://localhost:8400/callback` | `http://localhost` |
| Allow public client flows | Off | On |
| Client secret | Yes; the lifetime your policy sets | None |
| Permission type | Delegated only; no application permissions | Same |
| Consent | Admin consent for the delegated permissions below, or user consent for me | Same |
| Who signs in | Only me | Same |

Please send me the application (client) ID and the directory (tenant) ID,
and for a confidential client the client secret value.

### Delegated permissions

| Permission | What the EA uses it for | Required |
|---|---|---|
| `User.Read`, `offline_access` | Sign-in and a refresh token, so it runs without a new sign-in each hour | Yes |
| `Mail.ReadWrite` | Read the inbox, write drafts, file and archive mail, set categories | Yes |
| `MailboxSettings.Read` | My time zone and working hours | Yes |
| `Calendars.ReadWrite` | Read my free time; create an event only after I click Create | Yes (`Calendars.Read` gives a read-only install) |
| `Mail.Send` | Send a draft only after I click Send | No: without it, I send every draft from Outlook myself |
| `Tasks.ReadWrite` | Add items to my Microsoft To Do lists | No |
| `Contacts.Read`, `People.Read`, `Files.Read` | Requested by default, but the current code calls none of these APIs | No: leave them out |

With a confidential client, the setup step signs in once, reads which
permissions the app grants and from then on asks for those only. With a
public client, the sign-in asks for the EA's full default list unless the
`GRAPH_SCOPES` setting names the granted ones, so I set that line to the
permissions you grant before I sign in. Either way, a permission you leave
out turns that feature off; nothing else breaks.

## Where the data goes

- **Microsoft Graph to this machine.** A service on the machine above
  reads my mail and calendar. The working state (a task ledger, summaries,
  drafts in progress) stays on that machine.
- **The language model.** The EA uses Claude (Anthropic) through my own
  Claude subscription to understand mail and write replies. An optional
  self-hosted model can read raw mail first, so that Claude gets a summary
  instead of the text. A keep list holds chosen domains and senders away
  from Claude completely.
- **The chat interface.** I talk to the EA in a chat server that runs on
  the same machine (Zulip), or in Slack if I choose it.
- **No vendor server.** There is no service in the middle. The code runs
  on my machine, and nothing goes to the software's authors.

## What stops the EA from acting on its own

- **A separate operating-system user.** The model runs as its own Unix
  user. That user cannot read the Graph client secret or the token cache.
- **A mail mediator.** A service that runs as me holds the secret and the
  tokens. It carries out only the operations on an allowlist: reads, drafts
  and filing.
- **Code-enforced buttons.** Sending mail, creating, moving or deleting a
  calendar event, and scheduling a send each need a one-time token. The
  chat bot makes that token only when I click a button in my own chat. The
  model has no way to make one.
- **No deletion.** The EA can delete its own drafts only. It has no verb
  that deletes other mail.
- **Network limits.** On a hardened install, the model's user reaches
  only the hosts it needs.

The project's `docs/SECURITY.md` describes each layer.

## What IT can turn off

- Delete the client secret or the app registration. The EA stops at its
  next Graph call.
- Revoke my sessions. The refresh token stops working.
- Let the secret expire. I ask for a new one, or the EA stays off.

## Precedent

University IT groups provision app registrations of this kind on a
limited basis: a confidential client with a secret, delegated permissions,
admin consent, and public client flows off. The EA is built for that setup,
and works with a public client where policy allows one.

## After IT answers

Put the IDs in `~/.ea/.env` and the secret in `~/.ea-private/secrets.env`,
then run `python3 scripts/install/user_init.py --phase auth`. SETUP.md "Microsoft Graph app registration"
has the details.
