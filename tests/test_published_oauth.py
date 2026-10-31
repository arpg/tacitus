"""The project's published OAuth clients (docs/published-oauth-apps.md).

preprocessor/published_apps.py holds the ids of the multitenant Entra app
and the Google desktop client the project registers once. These tests
drive both states: with the ids empty (as committed until the maintainer
registers the apps) every client, the setup questions and the setup page
behave as before; with an id present, a client the user configured still
wins, the published app is a public client on the `organizations`
authority, the first sign-in is a loopback browser sign-in with PKCE or a
device code, setup offers it first, and the doctor names the path. MSAL
and Google are never called: the app objects are fakes.
"""
import importlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

# No test may touch production state: graph_client reads its token cache
# from EA_DIR at import, so EA_DIR is a scratch tree before the import.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-published-")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "scripts" / "install"))

import msal  # noqa: E402

import gmail_client  # noqa: E402
import google_calendar_client as gcal  # noqa: E402
import graph_client as gc  # noqa: E402
import published_apps  # noqa: E402
import setup_questions as Q  # noqa: E402

_spec = importlib.util.spec_from_file_location("ea_doctor_published", ROOT / "scripts" / "ea-doctor.py")
doctor = importlib.util.module_from_spec(_spec)
sys.modules["ea_doctor_published"] = doctor
_spec.loader.exec_module(doctor)

APP_ID = "00000000-aaaa-bbbb-cccc-000000000001"   # made up
OWN_ID = "00000000-aaaa-bbbb-cccc-000000000002"   # made up
GOOGLE_ID = "000000000000-madeup.apps.googleusercontent.com"
GRAPH_KEYS = ("GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_AUTH_METHOD", "GRAPH_AUTHORITY",
              "GRAPH_SCOPES", "GRAPH_REDIRECT_URI")
ORIGINAL_ENV = {k: os.environ.get(k) for k in GRAPH_KEYS + ("EA_DIR",)}
ID_NAMES = ("PUBLISHED_GRAPH_CLIENT_ID", "PUBLISHED_GOOGLE_CLIENT_ID",
            "PUBLISHED_GOOGLE_CLIENT_SECRET")
COMMITTED_IDS = {name: getattr(published_apps, name) for name in ID_NAMES}


@pytest.fixture(autouse=True)
def _unregistered_state():
    """Every test starts with the ids empty, the state before the maintainer
    registers the apps, whatever the committed values; a test that needs
    an id sets one. Afterwards the committed ids come back and graph_client
    and setup_questions are reloaded under the original environment. Set
    up before monkeypatch, so torn down after its undo."""
    for name in ID_NAMES:
        setattr(published_apps, name, "")
    yield
    for name, value in COMMITTED_IDS.items():
        setattr(published_apps, name, value)
    for k, v in ORIGINAL_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(gc)
    importlib.reload(Q)
    doctor.RESULTS.clear()


def _cache_with_refresh_token(client_id: str) -> str:
    """An MSAL cache holding one refresh token issued to `client_id`."""
    home = "uid.utid"
    return json.dumps({
        "Account": {f"{home}-login.microsoftonline.com-utid": {
            "home_account_id": home, "environment": "login.microsoftonline.com",
            "realm": "utid", "local_account_id": "uid", "username": "pat@example.edu",
            "authority_type": "MSSTS"}},
        "RefreshToken": {f"{home}-login.microsoftonline.com-refreshtoken-{client_id}--": {
            "home_account_id": home, "environment": "login.microsoftonline.com",
            "credential_type": "RefreshToken", "client_id": client_id, "secret": "made-up"}},
    })


def _built():
    """(kind, client id, authority) of the MSAL app graph_client built last.
    The suite's conftest replaces both MSAL classes with mocks, since a real
    one performs remote discovery."""
    for kind, cls in (("public", msal.PublicClientApplication),
                      ("confidential", msal.ConfidentialClientApplication)):
        if cls.call_args is not None:
            args, kwargs = cls.call_args
            return kind, args[0], kwargs.get("authority")
    return None


def _reload_gc(monkeypatch, tmp_path, published="", env=None, cache=None):
    """graph_client imported afresh with these published id, env and cache."""
    msal.PublicClientApplication.reset_mock()
    msal.ConfidentialClientApplication.reset_mock()
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", published)
    ea = tmp_path / "ea"
    (ea / "auth").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EA_DIR", str(ea))
    for k in GRAPH_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    if cache is not None:
        (ea / "auth" / "msal_token_cache.json").write_text(cache)
    return importlib.reload(gc)


# -- published_apps --------------------------------------------------------

def test_the_committed_ids_are_empty_or_well_formed():
    """Empty until the maintainer registers the apps; then an Entra
    application id and a Google client id."""
    graph, google = (COMMITTED_IDS["PUBLISHED_GRAPH_CLIENT_ID"],
                     COMMITTED_IDS["PUBLISHED_GOOGLE_CLIENT_ID"])
    assert graph == "" or __import__("re").fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", graph)
    assert google == "" or google.endswith(".apps.googleusercontent.com")


def test_client_id_order_own_registration_then_published_then_none(monkeypatch):
    assert published_apps.graph_client_id("") == ("", "")
    assert published_apps.graph_client_id(OWN_ID) == ("custom", OWN_ID)
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    assert published_apps.graph_client_id("") == ("published", APP_ID)
    assert published_apps.graph_client_id(OWN_ID) == ("custom", OWN_ID), "the user's own wins"


def test_scopes_are_least_per_profile_and_the_registration_covers_them_all():
    reg = published_apps.graph_registered_scopes()
    assert reg[:3] == ["openid", "profile", "offline_access"]
    for profile, scopes in published_apps.GRAPH_SCOPES_BY_PROFILE.items():
        assert set(scopes) <= set(reg), profile
    assert published_apps.graph_scopes("availability") == ["Calendars.ReadBasic"]
    assert "Mail.Send" not in published_apps.graph_scopes("email")
    assert not any("Write" in s or "Send" in s for s in published_apps.graph_scopes("review-only"))
    assert published_apps.graph_scopes(None) == list(published_apps.PUBLISHED_GRAPH_SCOPES)
    assert published_apps.graph_scopes("assistant") == list(published_apps.PUBLISHED_GRAPH_SCOPES)
    for unused in ("User.Read", "Contacts.Read", "People.Read", "Files.Read", "Tasks.ReadWrite"):
        assert unused not in reg


def test_admin_consent_url(monkeypatch):
    assert published_apps.admin_consent_url() == "", "no link while unregistered"
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    url = urllib.parse.urlsplit(published_apps.admin_consent_url())
    assert (url.scheme, url.netloc, url.path) == (
        "https", "login.microsoftonline.com", "/organizations/v2.0/adminconsent")
    q = dict(urllib.parse.parse_qsl(url.query))
    assert q["client_id"] == APP_ID
    assert q["redirect_uri"] == published_apps.PUBLISHED_GRAPH_CONSENT_REDIRECT
    scopes = q["scope"].split()
    assert scopes[:3] == ["openid", "profile", "offline_access"]
    assert "https://graph.microsoft.com/Mail.Send" in scopes
    one = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        published_apps.admin_consent_url("availability", tenant="tenant-guid")).query))
    assert one["scope"].split() == ["openid", "profile", "offline_access",
                                    "https://graph.microsoft.com/Calendars.ReadBasic"]
    assert "/tenant-guid/v2.0/adminconsent" in published_apps.admin_consent_url(tenant="tenant-guid")


def test_google_client_config_is_an_installed_client(monkeypatch):
    assert published_apps.google_client_config() is None
    monkeypatch.setattr(published_apps, "PUBLISHED_GOOGLE_CLIENT_ID", GOOGLE_ID)
    cfg = published_apps.google_client_config()
    assert set(cfg) == {"installed"}
    inst = cfg["installed"]
    assert inst["client_id"] == GOOGLE_ID and inst["client_secret"] == ""
    assert {"auth_uri", "token_uri", "client_id"} <= set(inst)
    assert inst["redirect_uris"] == ["http://localhost"]


# -- graph_client: which client --------------------------------------------

def test_with_the_id_empty_graph_client_is_unchanged(monkeypatch, tmp_path):
    m = _reload_gc(monkeypatch, tmp_path)
    assert m.CLIENT_PATH == "graph_powershell"
    assert m.WELL_KNOWN_CLIENTS == [("graph_powershell", m.GRAPH_POWERSHELL_CLIENT_ID)]
    assert m.AUTHORITY == "https://login.microsoftonline.com/common"
    assert m._effective_scopes() == m.SCOPES
    assert _built() == ("public", m.GRAPH_POWERSHELL_CLIENT_ID,
                        "https://login.microsoftonline.com/common")


def test_the_published_app_is_used_when_the_user_configured_none(monkeypatch, tmp_path):
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID)
    assert m.CLIENT_PATH == "published"
    assert m.WELL_KNOWN_CLIENTS == [("published", APP_ID)]
    assert m.AUTHORITY == published_apps.PUBLISHED_GRAPH_AUTHORITY
    assert _built() == ("public", APP_ID, published_apps.PUBLISHED_GRAPH_AUTHORITY)
    assert m._effective_scopes() == [f"https://graph.microsoft.com/{s}"
                                     for s in published_apps.PUBLISHED_GRAPH_SCOPES]


def test_the_users_own_registration_wins_over_the_published_app(monkeypatch, tmp_path):
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   env={"GRAPH_CLIENT_ID": OWN_ID, "GRAPH_CLIENT_SECRET": "made-up"})
    assert m.CLIENT_PATH == "custom"
    assert m._use_confidential() is True
    assert _built()[:2] == ("confidential", OWN_ID)
    assert m.AUTHORITY == "https://login.microsoftonline.com/common"
    assert m._effective_scopes() == m.SCOPES


def test_the_published_app_is_never_confidential(monkeypatch, tmp_path):
    """A secret left from Option A cannot turn the published app into a
    confidential client, whatever the method says."""
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   env={"GRAPH_CLIENT_SECRET": "made-up", "GRAPH_AUTH_METHOD": "confidential"})
    assert m.CLIENT_PATH == "published"
    assert m._use_confidential() is False
    assert _built() == ("public", APP_ID, published_apps.PUBLISHED_GRAPH_AUTHORITY)


def test_graph_scopes_and_a_named_tenant_still_win(monkeypatch, tmp_path):
    tenant = "https://login.microsoftonline.com/tenant-guid"
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   env={"GRAPH_SCOPES": "Calendars.ReadBasic", "GRAPH_AUTHORITY": tenant})
    assert m._effective_scopes() == ["https://graph.microsoft.com/Calendars.ReadBasic"]
    assert m.AUTHORITY == tenant
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   env={"GRAPH_AUTHORITY": "https://login.microsoftonline.com/common"})
    assert m.AUTHORITY == "https://login.microsoftonline.com/common", "an explicit common stays"


def test_a_cache_from_the_earlier_default_keeps_it(monkeypatch, tmp_path):
    """An install whose refresh token came from the earlier default keeps
    signing in silently with it after the published id appears."""
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   cache=_cache_with_refresh_token(gc.GRAPH_POWERSHELL_CLIENT_ID))
    assert m.CLIENT_PATH == "graph_powershell"
    assert _built()[:2] == ("public", m.GRAPH_POWERSHELL_CLIENT_ID)
    m = _reload_gc(monkeypatch, tmp_path, published=APP_ID,
                   cache=_cache_with_refresh_token(APP_ID))
    assert m.CLIENT_PATH == "published"


# -- graph_client: which flow ----------------------------------------------

@pytest.mark.parametrize("method,browser,want", [
    ("", True, "browser"), ("", False, "device_code"),
    ("device_code", True, "device_code"), ("interactive", False, "browser"),
])
def test_flow_choice(monkeypatch, method, browser, want):
    monkeypatch.setattr(gc, "GRAPH_AUTH_METHOD", method)
    monkeypatch.setattr(gc, "_browser_available", lambda: browser)
    assert gc._published_flow() == want


def test_browser_detection(monkeypatch):
    for k in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gc, "_is_wsl", lambda: False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert gc._browser_available() is False, "Linux with no display"
    monkeypatch.setenv("DISPLAY", ":0")

    def no_browser(*a, **k):
        raise gc.webbrowser.Error("none")
    monkeypatch.setattr(gc.webbrowser, "get", no_browser)
    assert gc._browser_available() is False, "a display but no browser"
    monkeypatch.setattr(gc.webbrowser, "get", lambda *a, **k: object())
    assert gc._browser_available() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    assert gc._browser_available() is True
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.1 50000 192.0.2.2 22")
    assert gc._browser_available() is False, "a Mac over SSH opens no browser for the user"


def test_the_redirect_is_the_registered_localhost_on_the_configured_port(monkeypatch):
    monkeypatch.setattr(gc, "GRAPH_REDIRECT_URI", "http://localhost:8400/callback")
    assert gc._published_redirect_uri() == "http://localhost:8400"
    monkeypatch.setattr(gc, "GRAPH_REDIRECT_URI", "http://127.0.0.1:9123/callback")
    assert gc._published_redirect_uri() == "http://localhost:9123"


class FakeApp:
    """The MSAL calls the published flows make, recorded."""

    def __init__(self, device_result=None, code_result=None):
        self.calls = []
        self.device_result = device_result or {"access_token": "device-token",
                                               "id_token_claims": {"preferred_username": "u"}}
        self.code_result = code_result or {"access_token": "browser-token"}

    def initiate_auth_code_flow(self, scopes, redirect_uri=None):
        self.calls.append(("initiate_auth_code_flow", tuple(scopes), redirect_uri))
        return {"auth_uri": "https://login.example/authorize?code_challenge=x",
                "state": "s", "code_verifier": "v", "redirect_uri": redirect_uri}

    def acquire_token_by_auth_code_flow(self, flow, response):
        self.calls.append(("acquire_token_by_auth_code_flow", response.get("code")))
        return self.code_result

    def initiate_device_flow(self, scopes):
        self.calls.append(("initiate_device_flow", tuple(scopes)))
        return {"user_code": "ABCD", "message": "Enter ABCD at the device login page."}

    def acquire_token_by_device_flow(self, flow):
        self.calls.append(("acquire_token_by_device_flow",))
        return self.device_result


@pytest.fixture
def published_gc(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(gc, "_app", app)
    monkeypatch.setattr(gc, "CLIENT_PATH", "published")
    monkeypatch.setattr(gc, "GRAPH_AUTH_METHOD", "")
    monkeypatch.setattr(gc, "GRAPH_SCOPES", [])
    monkeypatch.setattr(gc, "_save_cache", lambda cache: None)
    monkeypatch.setattr(gc.webbrowser, "open", lambda *a, **k: True)
    monkeypatch.setattr(gc, "_patch_wsl_browser", lambda: None)
    return app


def test_a_browser_sign_in_redeems_the_code_on_the_loopback_redirect(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "_browser_available", lambda: True)
    seen = {}

    def redirect(host, port, timeout=None):
        seen.update(host=host, port=port, timeout=timeout)
        return {"code": "the-code", "state": "s"}
    monkeypatch.setattr(gc, "_await_redirect", redirect)
    assert gc._published_seed() == "browser-token"
    kinds = [c[0] for c in published_gc.calls]
    assert kinds == ["initiate_auth_code_flow", "acquire_token_by_auth_code_flow"]
    assert published_gc.calls[0][2] == "http://localhost:8400"
    assert seen == {"host": "localhost", "port": 8400, "timeout": gc._PUBLISHED_BROWSER_WAIT}


def test_no_browser_means_a_device_code(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "_browser_available", lambda: False)
    monkeypatch.setattr(gc, "_loopback_sign_in", lambda *a, **k: pytest.fail("no browser here"))
    assert gc._published_seed() == "device-token"
    assert [c[0] for c in published_gc.calls] == ["initiate_device_flow",
                                                   "acquire_token_by_device_flow"]


def test_a_browser_that_never_redirects_falls_back_to_a_device_code(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "_browser_available", lambda: True)
    monkeypatch.setattr(gc, "_await_redirect", lambda host, port, timeout=None: {})
    assert gc._published_seed() == "device-token"
    assert [c[0] for c in published_gc.calls] == ["initiate_auth_code_flow",
                                                   "initiate_device_flow",
                                                   "acquire_token_by_device_flow"]


def test_a_taken_port_falls_back_to_a_device_code(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "_browser_available", lambda: True)

    def taken(host, port, timeout=None):
        raise OSError("Address already in use")
    monkeypatch.setattr(gc, "_await_redirect", taken)
    assert gc._published_seed() == "device-token"


def test_a_consent_error_is_final_and_names_the_it_request(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "_browser_available", lambda: True)
    monkeypatch.setattr(gc, "_await_redirect", lambda host, port, timeout=None: {
        "error": "consent_required",
        "error_description": "AADSTS65001: The user or administrator has not consented"})
    with pytest.raises(RuntimeError) as e:
        gc._published_seed()
    assert "docs/it-request.md" in str(e.value)
    assert "initiate_device_flow" not in [c[0] for c in published_gc.calls]


def test_a_device_code_blocked_by_conditional_access_says_how_to_sign_in(monkeypatch, published_gc):
    monkeypatch.setattr(gc, "GRAPH_AUTH_METHOD", "device_code")
    published_gc.device_result = {"error": "access_denied",
                                  "error_description": "AADSTS53003: Access has been blocked"}
    with pytest.raises(RuntimeError, match="Option D"):
        gc._published_seed()


def test_authenticate_routes_a_first_sign_in_to_the_published_app(monkeypatch, published_gc):
    published_gc.get_accounts = lambda: []
    monkeypatch.setattr(gc, "_source_token", lambda: None)
    monkeypatch.setattr(gc, "_SIGN_IN_REQUESTED", True)
    monkeypatch.setattr(gc, "_use_confidential", lambda: False)
    monkeypatch.setattr(gc, "_published_seed", lambda: "published-token")
    monkeypatch.setattr(gc, "_interactive_seed", lambda *a, **k: pytest.fail("not this path"))
    assert gc.authenticate() == "published-token"
    monkeypatch.setattr(gc, "CLIENT_PATH", "graph_powershell")
    monkeypatch.setattr(gc, "_interactive_seed", lambda *a, **k: "earlier-path")
    assert gc.authenticate() == "earlier-path"


def test_the_loopback_listener_returns_the_redirect_query():
    """_await_redirect, shared with the confidential flow: the first
    request's query, or {} after the timeout."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    def visit():
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=c1&state=s1", timeout=2).read()
                return
            except OSError:
                time.sleep(0.05)
    t = threading.Thread(target=visit)
    t.start()
    got = gc._await_redirect("127.0.0.1", port, timeout=10)
    t.join()
    assert got == {"code": "c1", "state": "s1"}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert gc._await_redirect("127.0.0.1", port, timeout=0.2) == {}


# -- the Google clients ----------------------------------------------------

@pytest.mark.parametrize("mod", [gcal, gmail_client], ids=["calendar", "gmail"])
def test_google_client_order(monkeypatch, tmp_path, mod):
    secret = tmp_path / "google_client_secret.json"
    monkeypatch.setattr(mod, "CLIENT_SECRET_FILE", secret)
    assert mod._client_source() == ""
    monkeypatch.setattr(published_apps, "PUBLISHED_GOOGLE_CLIENT_ID", GOOGLE_ID)
    assert mod._client_source() == "published"
    flow = mod._client_flow()
    assert flow.client_type == "installed" and flow.client_config["client_id"] == GOOGLE_ID
    flow.redirect_uri = "http://localhost:8400"
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["client_id"] == GOOGLE_ID and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://localhost:8400"
    secret.write_text(json.dumps({"installed": {
        "client_id": "own.apps.googleusercontent.com", "client_secret": "made-up",
        "auth_uri": published_apps.GOOGLE_AUTH_URI, "token_uri": published_apps.GOOGLE_TOKEN_URI,
        "redirect_uris": ["http://localhost"]}}))
    assert mod._client_source() == "file", "the user's own client wins"
    monkeypatch.setattr(mod, "_checked_token_io", lambda: False)
    assert mod._client_flow().client_config["client_id"] == "own.apps.googleusercontent.com"


@pytest.mark.parametrize("mod", [gcal, gmail_client], ids=["calendar", "gmail"])
def test_with_no_google_client_the_sign_in_stops_as_before(monkeypatch, tmp_path, capsys, mod):
    monkeypatch.setattr(mod, "CLIENT_SECRET_FILE", tmp_path / "google_client_secret.json")
    monkeypatch.setattr(mod, "_token_path", lambda email: tmp_path / "no-token.json")
    assert mod.authenticate_account("pat@example.com", interactive=True) is None
    assert "client secret not found" in capsys.readouterr().err


# -- setup -------------------------------------------------------------------

def _method_question(ph):
    return next(q for q in ph["questions"] if q["key"] == "graph-auth-method")


def test_setup_offers_the_published_sign_in_only_with_an_id(monkeypatch):
    importlib.reload(Q)
    ph = Q.phase("auth")
    q = _method_question(ph)
    assert [c[0] + ":" + c[2] for c in q["choices"]] == [
        "confidential:1", "device_code:2", "interactive:3", "skip:4"]
    assert q["label"] == "Graph auth method [1-4]" and q["default"] == [["literal", "1"]]
    assert not any("published" in line for line in ph["intro"])
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    importlib.reload(Q)
    ph = Q.phase("auth")
    q = _method_question(ph)
    assert [c[0] + ":" + c[2] for c in q["choices"]] == [
        "published:0", "confidential:1", "device_code:2", "interactive:3", "skip:4"], \
        "first, and the registration methods keep their numbers"
    assert q["label"] == "Graph auth method [0-4]"
    assert q["default"] == [["ctx", "graph_method"], ["literal", "0"]], \
        "an existing registration's method, else the published app"
    assert "0) published" in ph["intro"][4]


def test_the_published_choice_asks_no_registration_values(monkeypatch):
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    importlib.reload(Q)
    values, errors = Q.resolve("auth", {}, {"profile": "email"}, front="terminal")
    assert not errors and values["graph-auth-method"] == "published", "the default"
    assert "graph-client-id" not in values and "graph-client-secret" not in values
    assert values["graph-sign-in-now"] is True
    values, errors = Q.resolve("auth", {"graph-auth-method": "1"}, {"profile": "email"})
    assert values["graph-auth-method"] == "confidential"
    assert errors.get("graph-client-id"), "a registration still needs its id"


def test_an_existing_registration_keeps_its_method_as_the_default(monkeypatch, tmp_path):
    """On a second run, the published option does not displace the method
    ~/.ea/.env records for the user's own registration."""
    import user_init
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    importlib.reload(Q)
    monkeypatch.setattr(user_init, "EA_DIR", tmp_path)
    assert user_init.recorded_graph_method() == ""
    (tmp_path / ".env").write_text("GRAPH_AUTH_METHOD=device_code\nGRAPH_CLIENT_ID=" + OWN_ID + "\n")
    assert user_init.recorded_graph_method() == "device_code"
    q = _method_question(Q.phase("auth"))
    assert Q.default_of(q, {}, {"graph_method": "device_code"}) == "device_code"
    assert Q.default_of(q, {}, {"graph_method": ""}) == "0"
    (tmp_path / ".env").write_text("GRAPH_AUTH_METHOD=\nGRAPH_CLIENT_ID=\n")
    assert user_init.recorded_graph_method() == "", "the published path records no method"


def test_the_published_answer_writes_the_profiles_scopes(monkeypatch, tmp_path):
    import user_init
    ea = tmp_path / "ea"
    (ea / ".env").parent.mkdir(parents=True)
    (ea / ".env").write_text("GRAPH_CLIENT_ID=old\nGRAPH_AUTH_METHOD=confidential\nEA_MODEL=x\n")
    monkeypatch.setattr(user_init, "EA_DIR", ea)
    monkeypatch.setattr(user_init, "AUTH_DIR", ea / "auth")
    user_init.write_auth({"graph-auth-method": "published"}, {"profile": "email"})
    env = dict(line.split("=", 1) for line in (ea / ".env").read_text().splitlines())
    assert env["GRAPH_CLIENT_ID"] == "" and env["GRAPH_AUTH_METHOD"] == ""
    assert env["GRAPH_AUTHORITY"] == "" and env["EA_MODEL"] == "x"
    assert env["GRAPH_SCOPES"] == "Mail.ReadWrite Calendars.Read MailboxSettings.Read"


def test_the_setup_page_names_the_published_sign_in_only_with_an_id(monkeypatch):
    import setup_web

    def outlook(steps):
        return next(s for s in steps if s["title"].startswith("Outlook sign-in"))
    assert outlook(setup_web.next_steps({"profile": "email"}, "linux", {}))["title"] == \
        "Outlook sign-in"
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    step = outlook(setup_web.next_steps({"profile": "email"}, "linux", {}))
    assert step["title"] == "Outlook sign-in (no registration)"
    assert step["commands"] == ["python3 preprocessor/graph_client.py"]
    own = outlook(setup_web.next_steps({"profile": "email"}, "linux",
                                       {"GRAPH_CLIENT_ID": OWN_ID}))
    assert own["title"] == "Outlook sign-in", "an install with its own id is not told otherwise"


# -- the doctor --------------------------------------------------------------

def test_doctor_names_the_graph_path(monkeypatch, tmp_path):
    cache = tmp_path / "msal_token_cache.json"
    path = doctor.graph_sign_in_path
    assert "confidential client (Option A)" in path(
        {"GRAPH_CLIENT_ID": OWN_ID, "GRAPH_AUTH_METHOD": "confidential"}, cache, [])
    assert "confidential client (Option A)" in path(
        {"GRAPH_CLIENT_ID": OWN_ID, "GRAPH_CLIENT_SECRET": "made-up"}, cache, [])
    assert "public client (Option B, device_code)" in path(
        {"GRAPH_CLIENT_ID": OWN_ID, "GRAPH_AUTH_METHOD": "device_code"}, cache, [])
    assert "GRAPH_CLIENT_ID unset" in path({}, cache, [])
    assert "outlook-web-auth (Option C)" in path({}, cache, ["outlook-web-auth"])
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    assert path({}, cache, []) == "the Tacitus app, no registration (Option D)"
    assert "Option A" in path({"GRAPH_CLIENT_ID": OWN_ID, "GRAPH_AUTH_METHOD": "confidential"},
                              cache, []), "the user's own registration wins"
    cache.write_text(_cache_with_refresh_token(doctor.GRAPH_POWERSHELL_CLIENT_ID))
    assert "Graph PowerShell" in path({}, cache, [])
    assert doctor.GRAPH_POWERSHELL_CLIENT_ID == gc.GRAPH_POWERSHELL_CLIENT_ID


def test_doctor_check_auth_reports_the_rows(monkeypatch, tmp_path):
    ea = tmp_path / "ea"
    (ea / "auth").mkdir(parents=True)
    monkeypatch.setattr(doctor, "_graph_auth_sources", lambda: [])
    monkeypatch.setattr(doctor, "_backends", lambda ea_dir: {
        "mail": "graph", "calendar": "graph", "availability": "graph"})
    doctor.check_auth(ea, {})
    rows = {r["name"]: r for r in doctor.RESULTS}
    assert rows["auth: graph sign-in path"]["status"] == "PASS"
    assert "GRAPH_CLIENT_ID unset" in rows["auth: graph sign-in path"]["detail"]
    assert "auth: google client" not in rows, "no Google client: the tokens row speaks"
    doctor.RESULTS.clear()
    monkeypatch.setattr(published_apps, "PUBLISHED_GRAPH_CLIENT_ID", APP_ID)
    monkeypatch.setattr(published_apps, "PUBLISHED_GOOGLE_CLIENT_ID", GOOGLE_ID)
    doctor.check_auth(ea, {})
    rows = {r["name"]: r for r in doctor.RESULTS}
    assert rows["auth: graph sign-in path"]["detail"] == "the Tacitus app, no registration (Option D)"
    assert rows["auth: google client"]["detail"].startswith("the Tacitus desktop client")
    doctor.RESULTS.clear()
    (ea / "auth" / "google_client_secret.json").write_text("{}")
    doctor.check_auth(ea, {})
    rows = {r["name"]: r for r in doctor.RESULTS}
    assert rows["auth: google client"]["detail"].startswith("your Google Cloud client")
