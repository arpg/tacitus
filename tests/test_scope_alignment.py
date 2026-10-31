#!/usr/bin/env python3
"""Unit tests for graph_client scope alignment and auth-method selection.

Covers the confidential (auth-code + secret) path added for tenants that
permit an app registration but disable "Allow public client flows":
  - align_scopes(): keep / drop-redundant / downgrade-to-Read / drop-missing
  - _effective_scopes(): GRAPH_SCOPES override vs. the default SCOPES superset
  - _use_confidential(): method selection from env
"""

import importlib
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import graph_client as gc


def _short(scopes):
    return [gc._scope_basename(s) for s in scopes]


# --------------------------------------------------------------------------
# align_scopes
# --------------------------------------------------------------------------

def test_align_keeps_directly_granted():
    granted = {"Mail.Send", "Files.Read"}
    desired = ["https://graph.microsoft.com/Mail.Send",
               "https://graph.microsoft.com/Files.Read"]
    assert _short(gc.align_scopes(granted, desired)) == ["Mail.Send", "Files.Read"]


def test_align_drops_redundant_read_when_readwrite_granted():
    """Mail.Read is redundant if Mail.ReadWrite is granted — drop it, keep RW."""
    granted = {"Mail.ReadWrite"}
    desired = ["https://graph.microsoft.com/Mail.Read",
               "https://graph.microsoft.com/Mail.ReadWrite"]
    assert _short(gc.align_scopes(granted, desired)) == ["Mail.ReadWrite"]


def test_align_downgrades_readwrite_to_read_when_only_read_granted():
    """Want Calendars.ReadWrite but app only grants Calendars.Read -> downgrade."""
    granted = {"Calendars.Read"}
    desired = ["https://graph.microsoft.com/Calendars.ReadWrite"]
    assert _short(gc.align_scopes(granted, desired)) == ["Calendars.Read"]


def test_align_drops_ungranted_scope_with_no_fallback():
    granted = {"Mail.Send"}
    desired = ["https://graph.microsoft.com/MailboxSettings.Read",
               "https://graph.microsoft.com/People.Read",
               "https://graph.microsoft.com/Mail.Send"]
    assert _short(gc.align_scopes(granted, desired)) == ["Mail.Send"]


def test_align_matches_a_lab_apps_grants():
    """End-to-end against a real lab app's delegated permissions."""
    granted = {"Mail.ReadWrite", "Mail.Send", "Calendars.Read",
               "Contacts.Read", "Files.Read", "User.Read"}
    aligned = _short(gc.align_scopes(granted, gc.SCOPES))
    assert aligned == ["Mail.ReadWrite", "Mail.Send", "Calendars.Read",
                       "Contacts.Read", "Files.Read"]
    # The gaps we expect to be dropped are absent:
    for missing in ("MailboxSettings.Read", "Tasks.ReadWrite", "People.Read",
                    "Calendars.ReadWrite", "Mail.Read"):
        assert missing not in aligned


def test_align_empty_grants_yields_nothing():
    assert gc.align_scopes(set(), gc.SCOPES) == []


def test_align_accepts_scp_style_basenames():
    """`granted` may come straight from a token 'scp' claim (basenames)."""
    granted = "Mail.ReadWrite Files.Read".split()
    aligned = _short(gc.align_scopes(granted, gc.SCOPES))
    assert "Mail.ReadWrite" in aligned and "Files.Read" in aligned


def test_align_returns_full_urls():
    out = gc.align_scopes({"Mail.Send"}, ["Mail.Send"])
    assert out == ["https://graph.microsoft.com/Mail.Send"]


# --------------------------------------------------------------------------
# _effective_scopes (GRAPH_SCOPES override) and _use_confidential
# These read module-level env captured at import, so reload under patched env.
# --------------------------------------------------------------------------

def _reload_with_env(**env):
    # These are the user's own registration (Options A and B): with no
    # GRAPH_CLIENT_ID the project's published app, once registered, would
    # take its place (tests/test_published_oauth.py).
    env.setdefault("GRAPH_CLIENT_ID", "00000000-1111-2222-3333-444444444444")
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return importlib.reload(gc)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_effective_scopes_defaults_to_full_superset():
    m = _reload_with_env(GRAPH_SCOPES=None)
    assert m._effective_scopes() == m.SCOPES


def test_effective_scopes_uses_override_and_normalizes_shortnames():
    m = _reload_with_env(GRAPH_SCOPES="Mail.ReadWrite, Calendars.Read Files.Read")
    assert m._effective_scopes() == [
        "https://graph.microsoft.com/Mail.ReadWrite",
        "https://graph.microsoft.com/Calendars.Read",
        "https://graph.microsoft.com/Files.Read",
    ]


def test_effective_scopes_preserves_reserved_scopes():
    m = _reload_with_env(GRAPH_SCOPES="Mail.Send offline_access")
    assert "offline_access" in m._effective_scopes()


def test_use_confidential_true_when_secret_present():
    m = _reload_with_env(GRAPH_CLIENT_SECRET="x", GRAPH_AUTH_METHOD=None)
    assert m._use_confidential() is True


def test_use_confidential_false_without_secret():
    m = _reload_with_env(GRAPH_CLIENT_SECRET=None, GRAPH_AUTH_METHOD=None)
    assert m._use_confidential() is False


def test_explicit_method_overrides_secret_inference():
    m = _reload_with_env(GRAPH_CLIENT_SECRET="x", GRAPH_AUTH_METHOD="device_code")
    assert m._use_confidential() is False


def test_explicit_confidential_without_secret():
    m = _reload_with_env(GRAPH_CLIENT_SECRET=None, GRAPH_AUTH_METHOD="confidential")
    assert m._use_confidential() is True


if __name__ == "__main__":
    # Restore a clean module at the end so a stale reload can't leak env.
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    importlib.reload(gc)
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
