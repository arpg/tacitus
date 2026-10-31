"""The OAuth clients the Tacitus project registers once for every install.

An install with no app registration of its own signs in through these: one
multitenant Microsoft Entra application and one Google desktop OAuth client,
both public clients (PKCE, a loopback redirect, no confidential secret). The
design, the scopes, the consent each institution gives and the maintainer's
registration steps are in docs/published-oauth-apps.md.

The identifiers are empty until the maintainer registers the two apps and
commits their ids here. While they are empty, every client behaves exactly
as it does without this module: graph_client falls back to its earlier
default, the Google clients need ~/.ea/auth/google_client_secret.json, and
the setup questions offer no published option.

A client the user configured always wins: GRAPH_CLIENT_ID in ~/.ea/.env for
Microsoft, ~/.ea/auth/google_client_secret.json for Google.

This module imports nothing outside the standard library, so the installer,
the doctor and the clients can all read it.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

# -- Microsoft --------------------------------------------------------------

# The Application (client) ID of the project's multitenant Entra app
# ("Accounts in any organizational directory"), a public client with the
# "Mobile and desktop applications" redirect http://localhost. Empty until
# the maintainer registers it.
PUBLISHED_GRAPH_CLIENT_ID = ""

# Work and school accounts in any tenant, the audience the app registers.
# Personal Microsoft accounts are out: getSchedule does not support them.
PUBLISHED_GRAPH_AUTHORITY = "https://login.microsoftonline.com/organizations"

# The loopback redirect for the published app's browser sign-in. The app
# registers http://localhost; Entra ignores the port of a localhost
# redirect when it matches one, so any port works; a path would not match.
PUBLISHED_GRAPH_REDIRECT_PORT = 8400

# The page an administrator lands on after the admin-consent link: a
# Microsoft-hosted blank page that the app registers beside http://localhost.
PUBLISHED_GRAPH_CONSENT_REDIRECT = "https://login.microsoftonline.com/common/oauth2/nativeclient"

# The OpenID Connect scopes MSAL adds to every sign-in. A tenant that turned
# user consent off must consent to these too, so the admin-consent link
# names them.
GRAPH_SIGN_IN_SCOPES = ("openid", "profile", "offline_access")

# The delegated Graph permissions each install asks for, the least that
# serves its features:
#   availability  other people's free/busy only (getSchedule), for a Mac
#                 whose mail and calendars are native
#   review-only   read mail and the calendar; nothing is written
#   email         drafts, filing and categories; reads the calendar
#   assistant     the above, plus the Send and Create buttons
GRAPH_SCOPES_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "availability": ("Calendars.ReadBasic",),
    "review-only": ("Mail.Read", "Calendars.Read", "MailboxSettings.Read"),
    "email": ("Mail.ReadWrite", "Calendars.Read", "MailboxSettings.Read"),
    "assistant": ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite",
                  "MailboxSettings.Read"),
}

# What the published app asks for when GRAPH_SCOPES does not narrow it.
PUBLISHED_GRAPH_SCOPES: tuple[str, ...] = GRAPH_SCOPES_BY_PROFILE["assistant"]


def graph_registered_scopes() -> list[str]:
    """The delegated permissions the published registration lists (its
    required permissions): the sign-in scopes and every profile's scopes,
    so one tenant-wide consent covers whichever profile a user installs."""
    out = list(GRAPH_SIGN_IN_SCOPES)
    for scopes in GRAPH_SCOPES_BY_PROFILE.values():
        out += [s for s in scopes if s not in out]
    return out


def graph_scopes(profile: str | None) -> list[str]:
    """The delegated Graph permissions an install of `profile` asks for
    through the published app; the assistant's for an unknown profile."""
    return list(GRAPH_SCOPES_BY_PROFILE.get(profile or "", PUBLISHED_GRAPH_SCOPES))


def graph_client_id(configured: str = "") -> tuple[str, str]:
    """(path, client id) for an install whose GRAPH_CLIENT_ID is
    `configured`: "custom" for the user's own registration, "published" for
    the project's app, ("", "") when there is neither."""
    if configured:
        return "custom", configured
    if PUBLISHED_GRAPH_CLIENT_ID:
        return "published", PUBLISHED_GRAPH_CLIENT_ID
    return "", ""


# The profile whose scopes the admin-consent link asks for when none is
# named: drafts, filing and reading the calendar, no Send and no Create.
CONSENT_DEFAULT_PROFILE = "email"


def admin_consent_url(profile: str | None = None, tenant: str = "organizations") -> str:
    """The link an institution's administrator opens once to consent to the
    published app for the whole tenant (Microsoft's v2 admin-consent
    endpoint): the sign-in scopes and one profile's scopes, the email
    profile's unless `profile` names another. "all" asks for every scope
    the registration lists. "" while the app is not registered."""
    if not PUBLISHED_GRAPH_CLIENT_ID:
        return ""
    if profile == "all":
        names = graph_registered_scopes()
    else:
        names = list(GRAPH_SIGN_IN_SCOPES) + graph_scopes(profile or CONSENT_DEFAULT_PROFILE)
    scope = " ".join(n if n in GRAPH_SIGN_IN_SCOPES else f"https://graph.microsoft.com/{n}"
                     for n in names)
    query = urlencode({"client_id": PUBLISHED_GRAPH_CLIENT_ID, "scope": scope,
                       "redirect_uri": PUBLISHED_GRAPH_CONSENT_REDIRECT, "state": "tacitus"})
    return f"https://login.microsoftonline.com/{tenant}/v2.0/adminconsent?{query}"


# -- Google -----------------------------------------------------------------

# The project's OAuth client of the "Desktop app" type. Google issues a
# desktop client a client secret, says a desktop app cannot keep it
# confidential, and lists it as optional in the token request; the Python
# libraries send whatever value is here. Empty until the maintainer
# registers the client (docs/published-oauth-apps.md, "Maintainer steps").
PUBLISHED_GOOGLE_CLIENT_ID = ""
PUBLISHED_GOOGLE_CLIENT_SECRET = ""

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def google_client_config() -> dict | None:
    """The published desktop client in Google's client-secrets format (the
    `installed` type), or None while it is not registered."""
    if not PUBLISHED_GOOGLE_CLIENT_ID:
        return None
    return {"installed": {
        "client_id": PUBLISHED_GOOGLE_CLIENT_ID,
        "client_secret": PUBLISHED_GOOGLE_CLIENT_SECRET,
        "auth_uri": GOOGLE_AUTH_URI,
        "token_uri": GOOGLE_TOKEN_URI,
        "redirect_uris": ["http://localhost"],
    }}


def google_client_source(secret_file: Path) -> str:
    """Which Google client signs a new account in: "file" for the user's
    own client secret, "published" for the project's client, "" when there
    is neither."""
    if secret_file.exists():
        return "file"
    return "published" if PUBLISHED_GOOGLE_CLIENT_ID else ""


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="The project's published OAuth clients (docs/published-oauth-apps.md).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    url = sub.add_parser("admin-consent-url",
                         help="print the link an institution's administrator opens once")
    url.add_argument("--profile", choices=sorted(GRAPH_SCOPES_BY_PROFILE) + ["all"],
                     default=CONSENT_DEFAULT_PROFILE,
                     help="ask for this profile's scopes (default: email; assistant adds Send "
                          "and calendar writes; all: every scope the registration lists)")
    url.add_argument("--tenant", default="organizations",
                     help="the tenant id or domain (default: organizations)")
    args = ap.parse_args()
    link = admin_consent_url(args.profile, args.tenant)
    if not link:
        raise SystemExit("The Tacitus app is not registered yet: PUBLISHED_GRAPH_CLIENT_ID "
                         "is empty (docs/published-oauth-apps.md, 'Maintainer steps').")
    print(link)
