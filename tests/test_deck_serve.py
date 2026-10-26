#!/usr/bin/env python3
"""The deck-hosting boundary: deck_serve.py (owner-side rules) and the
`tailscale` and `curl` shims on ea-bot's PATH (scripts/deck-bin).

What must hold: the EA can only ever make a tailnet `serve` route, for a
plain slug, on its own port range, pointing at a socket its own user owns;
it cannot touch another user's route, cannot serve while a Funnel is on,
and the shim maps the owner's scripts' two write calls onto exactly those
verbs while refusing everything else (funnel, reset, up, set). The curl
shim sends build-deck.sh's closing check to the deck's loopback port, and
only for a route the mediated status verb shows is the resident's.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-deck-"))

import deck_serve as D  # noqa: E402

RESIDENT = 997
OTHER = 1000
CFG = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"host.example.ts.net:443": {"Handlers": {
        "/owner-deck": {"Proxy": "http://127.0.0.1:3033/owner-deck"},
        "/ea-deck": {"Proxy": "http://127.0.0.1:3050/ea-deck"},
        "/stale-ea": {"Proxy": "http://127.0.0.1:3051/stale-ea"},
        "/odd": {"Proxy": "https://example.com/"},
    }}},
}
LISTENERS = {3033: OTHER, 3050: RESIDENT, 3052: RESIDENT, 3053: OTHER}
uid_of = LISTENERS.get

PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0BEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000   997        0 1 0000000000000000 100 0 0 10 0
   1: 0100007F:0BD9 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 0000000000000000 100 0 0 10 0
   2: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 0000000000000000 100 0 0 10 0
   3: 0100007F:0BEB 0100007F:C350 01 00000000:00000000 00:00000000 00000000   997        0 1 0000000000000000 100 0 0 10 0
   4: C0A80105:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 0000000000000000 100 0 0 10 0
"""


def _refused(fn, *args, **kw) -> str:
    try:
        fn(*args, **kw)
    except D.Refused as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} was not refused")


def test_readers():
    r = D.routes(CFG)
    assert r["/owner-deck"] == 3033 and r["/ea-deck"] == 3050 and r["/odd"] is None
    assert D.routes({}) == {}
    assert not D.funnel_active(CFG)
    assert D.funnel_active({**CFG, "AllowFunnel": {"host.example.ts.net:443": True}})
    assert not D.funnel_active({**CFG, "AllowFunnel": {"host.example.ts.net:443": False}})
    ls = D.parse_listeners(PROC_NET_TCP)
    assert ls == {3050: 997, 3033: 1000, 8080: 0}, ls   # established + non-loopback dropped


def test_slug_and_port_rules():
    for good in ("a", "program-update-2026-09", "x1-y2"):
        assert D.check_slug(good) == good
    for bad in ("", "-lead", "trail-", "Upper", "under_score", "sp ace", "../x", "a" * 81):
        _refused(D.check_slug, bad)
    assert D.check_port(3050) == 3050 and D.check_port(3069) == 3069
    for bad in (3049, 3070, 80, 443, -1, "3050"):
        _refused(D.check_port, bad)


def test_validate_serve():
    # The good case: EA port, ea-bot's listener, path free or already ours.
    D.validate_serve("new-deck", 3052, CFG, uid_of, RESIDENT)
    D.validate_serve("ea-deck", 3050, CFG, uid_of, RESIDENT)       # rebuild, same port
    D.validate_serve("ea-deck", 3052, CFG, uid_of, RESIDENT)       # ours, re-pointed
    D.validate_serve("stale-ea", 3052, CFG, uid_of, RESIDENT)      # stale EA route re-used
    # Refusals name the rule.
    assert "outside the EA range" in _refused(D.validate_serve, "x", 3033, CFG, uid_of, RESIDENT)
    assert "nothing listens" in _refused(D.validate_serve, "x", 3051, CFG, uid_of, RESIDENT)
    assert "belongs to uid" in _refused(D.validate_serve, "x", 3053, CFG, uid_of, RESIDENT)
    assert "another user" in _refused(D.validate_serve, "owner-deck", 3052, CFG, uid_of, RESIDENT)
    funnel = {**CFG, "AllowFunnel": {"host.example.ts.net:443": True}}
    assert "Funnel" in _refused(D.validate_serve, "new-deck", 3052, funnel, uid_of, RESIDENT)
    _refused(D.validate_serve, "Bad Slug", 3052, CFG, uid_of, RESIDENT)


def test_validate_off():
    assert D.validate_off("nothing-here", CFG, uid_of, RESIDENT) is None
    assert D.validate_off("ea-deck", CFG, uid_of, RESIDENT) == 3050
    assert D.validate_off("stale-ea", CFG, uid_of, RESIDENT) == 3051
    assert "another user" in _refused(D.validate_off, "owner-deck", CFG, uid_of, RESIDENT)
    assert "another user" in _refused(D.validate_off, "odd", CFG, uid_of, RESIDENT)


def _shim():
    spec = importlib.util.spec_from_loader(
        "ea_tailscale_shim",
        importlib.machinery.SourceFileLoader("ea_tailscale_shim",
                                             str(REPO / "scripts" / "deck-bin" / "tailscale")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shim_plan():
    S = _shim()
    # Reads pass through untouched.
    for argv in (["version"], ["status", "--json"], ["serve", "status"],
                 ["serve", "status", "--json"], ["funnel", "status"], ["ip", "-4"]):
        assert S.plan(argv) == ("passthrough", argv), argv
    # The two writes the owner's scripts make.
    assert S.plan(["serve", "--bg", "--https=443", "--set-path=/my-deck",
                   "http://127.0.0.1:3050/my-deck"]) == \
        ("deck_serve", ["serve", "--slug", "my-deck", "--port", "3050"])
    assert S.plan(["serve", "--https", "443", "--set-path", "/my-deck",
                   "http://127.0.0.1:3050/my-deck"]) == \
        ("deck_serve", ["serve", "--slug", "my-deck", "--port", "3050"])
    assert S.plan(["serve", "--https=443", "--set-path=/my-deck", "off"]) == \
        ("deck_serve", ["off", "--slug", "my-deck"])
    # Everything else is refused, with the reason.
    for argv in (["funnel", "--bg", "--https=443", "--set-path=/x", "http://127.0.0.1:3050/x"],
                 ["funnel", "--https=443", "on"], ["serve", "reset"], ["up"], ["down"],
                 ["set", "--operator=ea-bot"], ["cert", "host.example.ts.net"], [],
                 ["serve", "--https=443", "http://127.0.0.1:3050"],           # no path
                 ["serve", "--https=8443", "--set-path=/x", "http://127.0.0.1:3050/x"],
                 ["serve", "--https=443", "--set-path=/x", "http://127.0.0.1:3050/y"],
                 ["serve", "--https=443", "--set-path=/x", "http://10.0.0.5:3050/x"],
                 ["serve", "--https=443", "--set-path=/x", "--yes", "http://127.0.0.1:3050/x"],
                 ["serve", "--https=443", "--set-path=/x"],
                 ["serve", "--https=443", "--set-path=/Bad", "http://127.0.0.1:3050/Bad"]):
        kind, reason = S.plan(argv)
        assert kind == "refuse", (argv, kind, reason)
        assert reason


def test_shim_binary_refuses_funnel_end_to_end():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "deck-bin" / "tailscale"),
                        "funnel", "--https=443", "on"], capture_output=True, text=True)
    assert r.returncode == 1 and "refused" in r.stderr


def _curl_shim():
    spec = importlib.util.spec_from_loader(
        "ea_curl_shim",
        importlib.machinery.SourceFileLoader("ea_curl_shim",
                                             str(REPO / "scripts" / "deck-bin" / "curl")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = ["-sS", "-o", "/dev/null", "-m", "20", "-w", "%{http_code}"]
STATUS = {"status": "ok", "verb": "status", "dns_name": "host.example.ts.net",
          "funnel_active": False, "port_range": [3050, 3069],
          "routes": [{"path": "/ea-deck", "port": 3052, "resident": True, "listening": True},
                     {"path": "/owner-deck", "port": 3033, "resident": False, "listening": True},
                     {"path": "/odd-port", "port": 8080, "resident": True, "listening": True}]}


def test_curl_shim_sends_the_deck_check_to_the_decks_loopback_port():
    """build-deck.sh's closing check reaches the deck's own server: the
    resident's network policy refuses it tailscale serve on the tailnet
    address, and the route is confirmed through the mediated status verb."""
    C = _curl_shim()
    url = "https://host.example.ts.net/ea-deck/"
    assert C.deck_url(CHECK + [url]) == (len(CHECK), "host.example.ts.net", "ea-deck")
    assert C.plan(CHECK + [url], STATUS) == CHECK + ["http://127.0.0.1:3052/ea-deck/"]
    assert C.plan(CHECK + ["https://HOST.example.ts.net./ea-deck"], STATUS) == \
        CHECK + ["http://127.0.0.1:3052/ea-deck/"]


def test_curl_shim_leaves_every_other_call_to_the_real_curl():
    C = _curl_shim()
    for url in ("https://host.example.ts.net/owner-deck/",     # not the resident's route
                "https://host.example.ts.net/odd-port/",       # outside the EA's port range
                "https://host.example.ts.net/no-route/",       # no route at all
                "https://other.example.ts.net/ea-deck/",       # another node
                "https://host.example.ts.net:8443/ea-deck/",   # another port
                "http://host.example.ts.net/ea-deck/",         # not https
                "https://u@host.example.ts.net/ea-deck/",      # a user part
                "https://host.example.ts.net/ea-deck/?x=1",    # a query
                "https://host.example.ts.net/ea-deck/sub/",    # deeper than the slug
                "https://host.example.ts.net/Bad/",
                "https://example.com/"):
        argv = CHECK + [url]
        assert C.plan(argv, STATUS) == argv, url
    two = CHECK + ["https://host.example.ts.net/ea-deck/", "https://example.com/"]
    assert C.deck_url(two) is None and C.plan(two, STATUS) == two
    one = CHECK + ["https://host.example.ts.net/ea-deck/"]
    for status in (None, {}, {"status": "error"}, dict(STATUS, dns_name=""),
                   dict(STATUS, routes=[dict(STATUS["routes"][0], resident=False)]),
                   dict(STATUS, routes=[dict(STATUS["routes"][0], port="3052")])):
        assert C.plan(one, status) == one, status


def test_both_shims_are_executable():
    """A deck plugin puts scripts/deck-bin first on the build PATH; both
    shims must be there and runnable (the plugin's own tests check the
    PATH order)."""
    for name in ("tailscale", "curl"):
        assert os.access(REPO / "scripts" / "deck-bin" / name, os.X_OK), name


def test_deck_serve_cli_refuses_before_touching_tailscale(monkeypatch=None):
    # A bad slug and a bad port are refused by argument checks alone; the
    # CLI never needs tailscale for that. Run as a subprocess with the
    # mediation shim disabled (not the resident user).
    env = dict(os.environ, EA_DIR=os.environ["EA_DIR"])
    env.pop("EA_FORCE_MEDIATE", None)
    for argv in (["serve", "--slug", "Bad", "--port", "3050"],
                 ["serve", "--slug", "ok", "--port", "80"]):
        r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "deck_serve.py"), *argv],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 2, (argv, r.stdout, r.stderr)
        msg = json.loads(r.stderr.strip().splitlines()[-1])
        assert msg["status"] == "error"


def test_mediation_tables_carry_deck_serve():
    import mail_mediation as M
    import mail_mediator as MM
    assert M.SAFE["deck_serve"] == {"serve", "off", "status"}
    assert M.is_safe("deck_serve", "serve") and M.is_safe("deck_serve", "off")
    assert not M.is_safe("deck_serve", "funnel") and not M.is_safe("deck_serve", "reset")
    assert not M.is_allowed("deck_serve", "funnel", ["funnel"])
    assert MM.TOOLS["deck_serve"].name == "deck_serve.py"


def test_deck_code_names_no_institution_theme():
    """The theme is config; the enable script hard codes no institution's
    theme (the deck tool itself lives in the slidev-decks plugin)."""
    for rel in ("deploy/enable-deck-builder.sh",):
        assert "cu-boulder" not in (REPO / rel).read_text(), rel
    script = REPO / "deploy" / "enable-deck-builder.sh"
    assert "deck_theme()" in script.read_text()
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
