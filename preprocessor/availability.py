#!/usr/bin/env python3
"""Other people's free/busy: the availability provider.

The scheduling engine asks one question of other people's calendars:
getSchedule's availability view over a window, one character per slot
(0 free, 1 tentative, 2 busy, 3 out of office, 4 working elsewhere), with
the reason when a calendar could not be read. scheduling_engine.
_schedule_detail is the one place it asks. This module answers under the
provider `availability.provider` in ea-config.json names, a choice of its
own beside `mail.backend` and `calendar.backend`
(docs/macos-native-accounts.md, "Other people's free/busy"):

  graph  Microsoft Graph getSchedule on the token graph_client holds: an
         app registration (SETUP.md Option A or B) or an institution's
         admin-consented app. The default, and the behaviour before this
         seam existed.
  owa    the same call on the token the outlook-web-auth owner plugin
         harvests from the owner's Outlook on the web session (SETUP.md
         "Option C: no registration (outlook-web-auth)"). graph_client
         asks plugin token sources after its own cache, so on an install
         with no registration this is the plugin's token; the provider
         first checks the plugin has one and says so when it has not.
  none   no one else's calendar is read. Every address answers "not read"
         with NONE_REASON, the solver treats each attendee as one whose
         calendar it does not know, and the options come from the owner's
         calendars plus what the thread states (offered times, a booking
         link's slots). The reply offers the times and the invitees pick.

With calendar.backend "macos", an address whose calendar the owner sees in
Calendar (the owner's own, or a colleague's shared calendar that the
owner-private scope file maps to the address) is read through EventKit
first, whatever the provider: LocalFirst.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROVIDERS = ("graph", "owa", "none")
DEFAULT = "graph"
NONE_REASON = ("not read: the availability provider is none, so the invitees "
               "pick from the times offered")
OWA_NO_TOKEN = ("not read: outlook-web-auth has no Outlook token now; run "
                "outlook_web.py login (SETUP.md 'Option C: no registration "
                "(outlook-web-auth)')")


def _complete(emails, detail: dict) -> dict:
    """Every address asked, once: a view the fetch did not return is an
    unread calendar, never a free one."""
    out = dict(detail or {})
    for email in emails:
        if not any(k.lower() == email.lower() for k in out):
            out[email] = {"view": "", "error": "no schedule returned"}
    return out


def _graph_fetch():
    import graph_client
    return graph_client.get_schedule_detail


class GraphAvailability:
    """getSchedule through graph_client: `fetch(emails, start, end,
    interval_minutes=...)`, by default graph_client.get_schedule_detail."""

    name = "graph"
    source = "Graph getSchedule"
    degraded = False

    def __init__(self, fetch=None):
        self._fetch = fetch

    def schedule_detail(self, emails, start, end, interval_minutes: int = 15) -> dict:
        if not emails:
            return {}
        fetch = self._fetch or _graph_fetch()
        return _complete(emails, fetch(list(emails), start, end, interval_minutes=interval_minutes))


def _owa_token() -> str | None:
    """A Graph or Outlook REST token from an enabled owner plugin's auth
    source (outlook-web-auth declares kind "graph"), or None."""
    try:
        import plugin_registry
        return next(iter(plugin_registry.owner_auth_tokens("graph")), None)
    except Exception:
        return None


class OwaAvailability(GraphAvailability):
    """getSchedule on the outlook-web-auth plugin's token."""

    name = "owa"
    source = "Graph getSchedule (Outlook on the web session)"

    def __init__(self, fetch=None, token_source=None):
        super().__init__(fetch)
        self._token_source = token_source or _owa_token

    def schedule_detail(self, emails, start, end, interval_minutes: int = 15) -> dict:
        if not emails:
            return {}
        if not self._token_source():
            return {e: {"view": "", "error": OWA_NO_TOKEN} for e in emails}
        return super().schedule_detail(emails, start, end, interval_minutes)


class NoAvailability:
    """No one else's calendar: the invitees pick."""

    name = "none"
    source = "none (the invitees pick)"
    degraded = True

    def schedule_detail(self, emails, start, end, interval_minutes: int = 15) -> dict:
        return {e: {"view": "", "error": NONE_REASON} for e in emails}


class LocalFirst:
    """The calendars on this Mac first: `native` (a NativeCalendarClient)
    answers each address it can read; `rest` answers the others. The name,
    source and mode are the rest provider's, since that decides what the
    reply may claim about the people the Mac cannot see."""

    def __init__(self, native, rest):
        self.native = native
        self.rest = rest
        self.name = rest.name
        self.source = rest.source
        self.degraded = rest.degraded

    def schedule_detail(self, emails, start, end, interval_minutes: int = 15) -> dict:
        local = [e for e in emails if self.native.can_read(e)]
        others = [e for e in emails if e not in local]
        out = {}
        if local:
            out.update(self.native.get_schedule_detail(local, start, end, interval_minutes))
        if others:
            out.update(self.rest.schedule_detail(others, start, end, interval_minutes))
        return out


def provider_name(config_path=None) -> str:
    """The configured provider; an unreadable or unknown value is the
    default, the behaviour before the seam."""
    try:
        import native_accounts
        name = native_accounts.backends(config_path).get("availability", DEFAULT)
    except Exception:
        return DEFAULT
    return name if name in PROVIDERS else DEFAULT


def get_provider(name: str | None = None, *, fetch=None, token_source=None, native=None,
                 config_path=None):
    """The provider object for `name` (default: the configured one), with
    the Mac's own calendars first when `native` is given or the calendar
    backend is macos."""
    name = name or provider_name(config_path)
    if name == "none":
        rest = NoAvailability()
    elif name == "owa":
        rest = OwaAvailability(fetch, token_source)
    else:
        rest = GraphAvailability(fetch)
    if native is None:
        native = _native_calendar(config_path)
    return LocalFirst(native, rest) if native else rest


def _native_calendar(config_path=None):
    """A NativeCalendarClient when this owner process runs the macos
    calendar backend, else None."""
    if sys.platform != "darwin":
        return None
    try:
        import native_accounts
        if native_accounts.backends(config_path).get("calendar") != "macos":
            return None
        return native_accounts.calendar_client()
    except Exception:
        return None


def schedule_detail(emails, start, end, interval_minutes: int = 15, *, fetch=None,
                    config_path=None) -> dict:
    """{email: {"view", "error"}} for every address, under the configured
    provider. scheduling_engine passes graph_client's fetch, so a test that
    patches it is what answers."""
    return get_provider(fetch=fetch, config_path=config_path).schedule_detail(
        list(emails), start, end, interval_minutes)


def source_label(config_path=None) -> str:
    """What answers attendee free/busy, for the solver's output."""
    return {"graph": GraphAvailability.source, "owa": OwaAvailability.source,
            "none": NoAvailability.source}[provider_name(config_path)]


def mode(config_path=None) -> dict:
    """{"provider", "mode"} for the solver's output: "checked" when the
    provider reads other people's calendars, "invitee-picks" when it does
    not."""
    name = provider_name(config_path)
    return {"provider": name, "mode": "invitee-picks" if name == "none" else "checked"}
