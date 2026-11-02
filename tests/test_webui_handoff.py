"""A computer set up through the relay (docs/webchat-relay.md, Pairing): the
web app's side of the handoff.

The owner's device asks the bot for a handoff under "Add a device", "A
computer": it shows the short code, the address to open and when the code
ends, and, once the computer that took the code waits, its six-digit code
beside Approve and the card-button choice. On a phone through the relay
only that way is offered (`owner_routes` "handoff"). On the website's copy
of the app (a host with no bot behind it and a list of relays), the
computer types the code: the app derives the mailbox and the key with
PBKDF2 in WebCrypto, takes the sealed pairing from the relay once, opens it
and pairs as from a link, with no secret in its address. Once approved, it
takes the bot's signed release through the sealed channel and runs that,
even when the website serves another release.

The handoff's crypto is checked against the shared vectors
(tests/fixtures/relay_vectors.json, `handoff`: made by relay_crypto at 1,000
iterations, with the production values at 600,000), and the full runs use
the bot's own handoff (POST /api/relay/handoff) and the real relay's
mailbox. The rest of the app's tests are in tests/test_webui.py, whose
browser fixtures this file borrows.
"""
import base64
import functools
import json
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "botcore"))

from test_webui import (  # noqa: E402,F401  (the fixtures and helpers)
    WEBUI, _kept, _owner_context, _pair_and_approve, _worker_state, app, browser, relay_bot,
    stub_factory, wait_for,
)

VECTORS = json.loads((TESTS / "fixtures" / "relay_vectors.json").read_text())["handoff"]
CODE = "7K3QX-M9P0D"  # a made-up code: ten symbols of the alphabet
TYPED = " 7k3qx m9pod "  # as a person might type it, an O for the 0


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rc():
    pytest.importorskip("cryptography")
    from botcore.chat import relay_crypto
    return relay_crypto


def seal(code: str, payload: dict, relay: str) -> tuple[str, str]:
    """(mailbox at `relay`, blob) of a handoff, made by relay_crypto as the bot makes them."""
    rc = _rc()
    mailbox, key = rc.handoff_keys(code, relay)
    return mailbox, rc.seal_handoff(mailbox, key, json.dumps(payload).encode())


def made_up_payload(**over) -> dict:
    obj = {"v": 2, "r": "https://relay.example.org", "i": "a" * 26, "k": b64(bytes([1]) * 32),
           "c": "c" * 43, "p": "0" * 32, "s": b64(bytes(32)), "x": 1893456000,
           "a": "https://app.example.org/", "b": "b" * 64}
    obj.update(over)
    return obj


def test_the_handoff_crypto_in_webcrypto_matches_the_shared_vectors(app):
    """The code's normalisation (each pair of the vectors), the secret from
    one PBKDF2 block at the vectors' 1,000 iterations and at the app's own
    600,000, the mailbox per relay and the key from it by HMAC, and the
    sealed pairing, byte for byte as relay_crypto made them. A changed blob
    or another mailbox does not open."""
    v = VECTORS
    rc = _rc()
    assert v["iterations"] == 1000 and v["production"]["iterations"] == 600000
    assert v["salt"].encode() == rc.HANDOFF_PROTOCOL
    assert base64.urlsafe_b64decode(v["aad"] + "==") == v["salt"].encode() + v["mailbox"].encode()
    assert base64.urlsafe_b64decode(v["blob"] + "==")[:12].hex() == v["nonce"]
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    out = page.evaluate("""async ([v, pairs]) => {
        const said = pairs.map(([typed]) => handoffCode(typed));
        const secret = await handoffSecret(handoffCode(v.code), v.iterations);
        const keys = await handoffKeys(secret, v.relay);
        const other = await handoffKeys(secret, v.other_relay.relay);
        const madeSecret = await handoffSecret(v.normalized); // the app's own count
        const made = await handoffKeys(madeSecret, v.relay);
        const opened = await openHandoff(keys, v.blob);
        const flip = v.blob.slice(0, -2) + (v.blob.at(-2) === 'A' ? 'B' : 'A') + v.blob.at(-1);
        const bent = await openHandoff(keys, flip).then(() => 'opened', (e) => e.name);
        const elsewhere = await openHandoff({ ...keys, mailbox: other.mailbox }, v.blob).then(() => 'opened', (e) => e.name);
        return { said, normalized: handoffCode(v.code), secret: hex(secret), mailbox: keys.mailbox,
                 key: hex(keys.key), other: other.mailbox, otherKey: hex(other.key),
                 production: { secret: hex(madeSecret), mailbox: made.mailbox, key: hex(made.key) },
                 opened, bent, elsewhere };
    }""", [v, v["normalize"]])
    assert out["said"] == [want or "" for _, want in v["normalize"]]
    assert out["normalized"] == v["normalized"]
    assert (out["secret"], out["mailbox"], out["key"]) == (v["secret"], v["mailbox"], v["key"])
    assert out["other"] == v["other_relay"]["mailbox"] != v["mailbox"] and out["otherKey"] == v["key"]
    assert out["production"] == {k: v["production"][k] for k in ("secret", "mailbox", "key")}
    assert out["opened"] == json.loads(v["payload"])
    assert out["bent"] == "OperationError" and out["elsewhere"] == "OperationError"  # the mailbox is bound in
    assert not errors, errors


def _hosted_device(**over):
    return {"id": "4" * 16, "name": "Windows browser", "paired_at": 1793400000, "origin": "hosted",
            "pending": True, "code": "123456", "cards_allowed": False, "bundle_verified": False,
            "handoff": True, "by": "owner", "pairing": "7" * 32, **over}


def test_a_computer_shows_the_code_and_the_owner_approves_it(app):
    """"A computer" asks the bot for a handoff once, shows the code, the
    address without https:// and the warning; when the computer waits, its
    code shows beside Approve, card buttons off unless the owner ticks the
    box, and Approve sends the choice."""
    from playwright.sync_api import expect
    page, stub, errors = app(relay_pairing=True, handoff=True)
    page.wait_for_selector(".thread-item")
    expect(page.locator("#pair-phone")).to_have_text("Add a device")
    page.click("#pair-phone")
    expect(page.locator("#add-ways")).to_be_visible()
    expect(page.locator("#way-here")).to_be_hidden()  # not on the bot's own address
    expect(page.locator("#way-phone")).to_have_attribute("aria-pressed", "true")
    assert stub.handoffs == []
    page.click("#way-computer")
    expect(page.locator("#handoff-code")).to_have_text("ABCDE-FGHJK")
    expect(page.locator("#add-phone")).to_be_hidden()
    expect(page.locator("#handoff-open")).to_have_text("app.example.org/?relay=https%3A%2F%2Frelay.example.net")
    expect(page.locator("#handoff-note")).to_contain_text("The code works once, until ")
    expect(page.locator("#handoff-note")).to_contain_text("Never send this code through chat or mail.")
    expect(page.locator("#handoff-wait")).to_be_hidden()
    page.click("#way-phone")
    page.click("#way-computer")
    assert stub.handoffs == [{}]  # one code while it lasts

    # Another computer waits too (an older code's): the panel names the one
    # whose pairing this code made, whatever the order.
    stub.devices[:] = [_hosted_device(id="5" * 16, code="654321", pairing="8" * 32), _hosted_device()]
    wait = page.locator("#handoff-wait")
    expect(wait).to_be_visible(timeout=6000)
    expect(page.locator("#handoff-ask")).to_have_text("The computer shows a six-digit code. Tap Approve if it is 123 456.")
    expect(page.locator("#handoff-cards")).not_to_be_checked()
    row = page.locator("#phone-devices li[data-device='4444444444444444']")
    expect(row.locator("label")).to_have_text("Allow Send and Create buttons on this computer")
    page.check("#handoff-cards")
    expect(row.locator("input[type=checkbox]")).to_be_checked()
    page.click("#handoff-approve")
    wait_for(lambda: stub.approvals, 5)
    assert stub.approvals == [{"device": "4" * 16, "cards": True}]
    expect(wait).to_be_hidden()  # the other code's computer still waits: not this code's
    expect(row).to_contain_text("opened with a code, app not checked")
    assert not errors, errors


def test_a_phone_through_the_relay_offers_only_a_computer(app):
    """Where the bot lets this device start a handoff and nothing else
    (`owner_routes` "handoff"), "Add a device" opens on A computer alone and
    makes no phone pairing; with "none" the drawer offers nothing."""
    from playwright.sync_api import expect
    page, stub, errors = app(relay_pairing=True, handoff=True, owner_routes="handoff")
    page.wait_for_selector(".thread-item")
    page.click("#pair-phone")
    expect(page.locator("#handoff-code")).to_have_text("ABCDE-FGHJK")
    expect(page.locator("#add-ways")).to_be_hidden()
    expect(page.locator("#add-computer")).to_be_visible()
    assert stub.pairs == [] and stub.handoffs == [{}]
    assert not errors, errors
    page, stub, errors = app(relay_pairing=True, handoff=True, owner_routes="none")
    page.wait_for_selector(".thread-item")
    expect(page.locator("#pair-phone")).to_be_hidden()


# -- the website's copy of the app -------------------------------------------

class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


WEBSITE_RELEASE = "v9.9.9 (the website's release.txt)\n"
WEBSITE_INDEX = b"\n<!-- the website's own index -->\n"


def _website(root: Path, relays: list[str], changed: bytes = b"", index: bytes = b""):
    """A static host with no headers and no API (as the website is), serving
    a copy of the app (its app.css with `changed` added, its index.html with
    `index`), relays.json and release.txt. (server, its address)."""
    root.mkdir()
    for f in WEBUI.iterdir():
        shutil.copy(f, root / f.name)
    (root / "app.css").write_bytes((WEBUI / "app.css").read_bytes() + changed)
    (root / "index.html").write_bytes((WEBUI / "index.html").read_bytes() + index)
    (root / "relays.json").write_text(json.dumps(relays))
    (root / "release.txt").write_text(WEBSITE_RELEASE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # localhost: a secure context of its own origin, apart from the relay on 127.0.0.1.
    return srv, f"http://localhost:{srv.server_address[1]}/"


class _StubRelay(BaseHTTPRequestHandler):
    """GET /h/<mailbox> answers each sealed pairing once; a hello to /r/<install>/c is kept and refused.
    With `junk`, every lookup answers 200 with a blob that opens under no key."""
    boxes: dict = {}
    asked: list = []
    hellos: list = []
    junk = False

    def log_message(self, *args):
        pass

    def _out(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, x-relay-key")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._out(200, {})

    def do_GET(self):
        m = re.fullmatch(r"/h/([0-9a-f]{32})", self.path)
        if m:
            self.asked.append(m.group(1))
        blob = self.boxes.pop(m.group(1), None) if m else None
        blob = blob or (b64(bytes(64)) if m and self.junk else None)
        self._out(200, {"c": blob}) if blob else self._out(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.hellos.append({"path": self.path, "key": self.headers.get("X-Relay-Key"), "frame": body})
        self._out(200, {"k": "err", "e": "auth", "id": body.get("id")})


def test_the_computer_screen_takes_a_code_once_and_never_shows_the_secret(browser, tmp_path):
    """On the website's copy, with no pairing, the app asks for the code.
    A typo and a wrong code are refused in plain words; the right code,
    typed loosely, takes the sealed pairing from the relay once, and the
    hello goes to the relay the pairing names with its pairing id, while the
    address bar never holds a secret. A pairing for another address is
    refused. The website lists two relays, and the first answers every
    lookup with junk: it neither ends the search nor sees the mailbox the
    second relay holds (each relay sees a mailbox of its own)."""
    from playwright.sync_api import expect

    def stub(junk=False):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("R", (_StubRelay,), {
            "boxes": {}, "asked": [], "hellos": [], "junk": junk}))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"
    liar, jbase = stub(junk=True)
    relay, rbase = stub()
    site, url = _website(tmp_path / "site", [jbase, rbase + "/"])
    R, J = relay.RequestHandlerClass, liar.RequestHandlerClass
    payload = made_up_payload(r=rbase, a=url, p="5" * 32)
    mailbox, blob = seal(CODE, payload, rbase)
    R.boxes[mailbox] = blob
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        errors, urls = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("framenavigated", lambda f: urls.append(f.url))
        page.goto(url)
        expect(page.locator("#setup")).to_be_visible(timeout=10000)
        expect(page.locator("#setup h2")).to_have_text("Set up this computer")
        page.fill("#setup-code", "ABCDE")
        page.click("#setup-go")
        expect(page.locator("#setup-error")).to_have_text("A code has ten letters and digits, like ABCDE-FGHJK.")
        page.fill("#setup-code", "ZZZZZ-ZZZZZ")
        page.click("#setup-go")
        expect(page.locator("#setup-error")).to_have_text("That code did not work. Check it, or make a new one.", timeout=10000)
        assert R.asked == [_rc().handoff_keys("ZZZZZZZZZZ", rbase)[0]] and R.hellos == []
        assert J.asked == [_rc().handoff_keys("ZZZZZZZZZZ", jbase)[0]] != R.asked

        page.fill("#setup-code", TYPED)
        page.click("#setup-go")
        wait_for(lambda: R.hellos, 15)
        (hello,) = R.hellos
        assert hello["path"] == f"/r/{payload['i']}/c" and hello["key"] == payload["c"]
        frame = base64.urlsafe_b64decode(hello["frame"]["c"] + "==")
        assert hello["frame"]["k"] == "hello" and frame[:18] == bytes([1, 1]) + bytes.fromhex(payload["p"])
        expect(page.locator("#setup-error")).to_have_text("Setup did not finish. Make a new code and try again.", timeout=10000)
        assert R.asked[-1] == mailbox and mailbox not in R.boxes  # read once
        assert mailbox not in J.asked and J.asked[-1] == _rc().handoff_keys(CODE, jbase)[0]
        assert J.hellos == []
        assert page.evaluate("loadPairing().then((r) => r === undefined)")  # a failed pairing keeps nothing
        assert all("#" not in u and payload["s"] not in u for u in urls), urls

        other = made_up_payload(r=rbase, a="https://app.example.org/")
        box, sealed = seal("1111111111", other, rbase)
        R.boxes[box] = sealed
        page.fill("#setup-code", "11111-11111")
        page.click("#setup-go")
        expect(page.locator("#setup-error")).to_contain_text("That code is for Tacitus at another address.", timeout=10000)
        assert len(R.hellos) == 1
        assert not errors, errors
    finally:
        ctx.close()
        site.shutdown()
        relay.shutdown()
        liar.shutdown()


def _hosted(relay_bot, tmp_path, changed: bytes = b"", index: bytes = b""):
    """The website's copy of the app beside relay_bot, listing its relay,
    and the bot's relay block naming it as `hosted_app` (an http origin on
    localhost here, which the config check would refuse; the address the
    computer opens is all the handoff takes from it). (server, address)."""
    site, url = _website(tmp_path / "site", [relay_bot["rbase"]], changed, index)
    relay_bot["fe"].relay.hosted_app = url
    return site, url


def _computer(browser, url, code, again=False):
    """A computer at the website's copy types `code`; with `again` it opens
    the address twice first, so the worker already serves (and keeps) the
    website's page. (context, page, errors, URLs it asked, the addresses its
    page loaded)."""
    from playwright.sync_api import expect
    ctx = browser.new_context()
    page = ctx.new_page()
    errors, asked, loads = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: asked.append(r.url))
    page.on("load", lambda p: loads.append(p.url))
    page.goto(url)
    if again:
        page.evaluate("navigator.serviceWorker.ready.then(() => null)")
        page.reload()
        assert page.evaluate("Boolean(navigator.serviceWorker.controller)")
        loads.clear()
    expect(page.locator("#setup")).to_be_visible(timeout=10000)
    page.fill("#setup-code", code.lower().replace("-", " "))  # typed loosely
    page.click("#setup-go")
    expect(page.locator("#relay-note")).to_contain_text("This computer waits for approval", timeout=20000)
    return ctx, page, errors, asked, loads


def _runs_the_bots_release(page, fe, device):
    """The approved computer took the bot's signed files through the sealed
    channel, keeps and runs them, and attested the bot's release."""
    from playwright.sync_api import expect
    expect(page.locator(".chan .c-name").first).to_be_visible(timeout=40000)
    expect(page.locator("h1")).not_to_have_text("Tacitus stopped")
    bot_css = (WEBUI / "app.css").read_text()
    assert _kept(page, "app.css") == bot_css
    assert page.evaluate("fetch('app.css').then((r) => r.text())") == bot_css
    assert _worker_state(page, "trusted")["hash"] == fe.bundle_hash()
    wait_for(lambda: fe.relay.store.device_by_id(device).get("attested_bundle") == fe.bundle_hash(), 15)


def test_a_computer_sets_up_with_a_code_and_runs_the_bots_release(browser, relay_bot, tmp_path):
    """The owner opens "Add a device", "A computer" in the web chat: the bot
    makes the code and leaves the sealed pairing with the real relay, and
    the address to open is the website's copy (which lists the relay, so no
    ?relay=). The website serves another release than the bot (its app.css
    differs). The computer types the code, takes the pairing from the relay
    once, pairs and waits with its code; the owner's dialog shows that code
    beside Approve. Once approved, the computer takes the bot's signed files
    through the sealed channel and runs them: it does not stop.

    The computer opens the address with ?relay= and the website's index.html
    differs too: no copy kept under that address (the website's index)
    outlives the swap. Its release.txt, and a stray link, open without
    stopping the app."""
    from urllib.parse import quote
    from playwright.sync_api import expect
    fe, rbase = relay_bot["fe"], relay_bot["rbase"]
    site, url = _hosted(relay_bot, tmp_path, b"\n/* the website's own release */\n", WEBSITE_INDEX)
    contexts = []
    try:
        tail = _owner_context(browser, fe, contexts).new_page()
        tail.goto(relay_bot["public"] + "/")
        tail.click("#pair-phone")
        expect(tail.locator("#way-computer")).to_be_visible()
        tail.click("#way-computer")
        expect(tail.locator("#handoff-code")).to_have_text(re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$"))
        code = tail.inner_text("#handoff-code")
        expect(tail.locator("#handoff-open")).to_have_text(url)  # http here: nothing to leave out
        mailbox = _rc().handoff_keys(code, rbase)[0]
        assert list(relay_bot["relay"].handoffs) == [mailbox]  # left with the real relay
        assert relay_bot["relay"].handoffs[mailbox][0] == fe.relay.install_id

        shown = f"{url}?relay={quote(rbase, safe='')}"
        ctx, page, errors, asked, _ = _computer(browser, shown, code, again=True)
        contexts.append(ctx)
        (d,) = fe.relay.devices()
        assert (d["pending"], d["origin"], d["handoff"], d["by"]) == (True, "hosted", True, "owner")
        expect(page.locator("#relay-note")).to_contain_text(f"code {d['code'][:3]} {d['code'][3:]}")
        assert [u for u in asked if "/h/" in u] == [f"{rbase}/h/{mailbox}"]
        assert not relay_bot["relay"].handoffs  # read once
        assert page.evaluate("loadPairing().then((r) => [r.hosted, r.bundle])") == [True, fe.bundle_hash()]

        expect(tail.locator("#handoff-ask")).to_have_text(
            f"The computer shows a six-digit code. Tap Approve if it is {d['code'][:3]} {d['code'][3:]}.", timeout=10000)
        expect(tail.locator("#handoff-cards")).not_to_be_checked()
        tail.click("#handoff-approve")
        wait_for(lambda: not fe.relay.devices()[0]["pending"], 10)
        assert fe.relay.store.device_by_id(d["id"]).get("cards_allowed") is not True
        _runs_the_bots_release(page, fe, d["id"])
        assert "tr2=" not in page.url and code.replace("-", "") not in page.url
        # Every kept copy is a file of the bot's manifest, at its exact address.
        keys = page.evaluate("caches.open('tacitus-webchat-v3').then((c) => c.keys()).then((r) => r.map((q) => q.url))")
        assert keys and not [k for k in keys if "?" in k or "relay=" in k], keys
        page.goto(shown)
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=20000)
        assert "the website's own index" not in page.content()  # the bot's index, not the website's
        # The website's release.txt and a stray link stop nothing.
        page.goto(url + "release.txt")
        assert page.inner_text("body").strip() == WEBSITE_RELEASE.strip()
        assert page.goto(url + "stray.html").status == 404
        page.goto(url)
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=20000)
        assert _worker_state(page, "refused") is None
        assert _worker_state(page, "trusted")["hash"] == fe.bundle_hash()
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()
        site.shutdown()


def test_a_phone_through_the_relay_adds_a_computer(browser, relay_bot, tmp_path):
    """The owner's own phone, approved and off the tailnet, reaches the bot
    through the relay: "Add a device" offers A computer alone, the bot makes
    the code for it (`by`, that phone), the computer takes it, and the phone
    approves the computer it added. The owner hears of both in the DM. The
    website serves the bot's own release: the computer still loads its page
    once more after approval, so the bot's security policy runs it."""
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    site, url = _hosted(relay_bot, tmp_path)
    contexts = []
    try:
        _tail, phone, phone_errors, _asked = _pair_and_approve(browser, relay_bot, contexts)
        (own,) = fe.relay.devices()
        assert phone.evaluate("() => T.relay") is True
        assert phone.evaluate("() => [S.me.owner_routes, S.me.handoff]") == ["handoff", True]
        phone.click("#pair-phone")
        expect(phone.locator("#add-ways")).to_be_hidden()
        expect(phone.locator("#add-computer")).to_be_visible()
        expect(phone.locator("#handoff-code")).to_have_text(re.compile(r"^\w{5}-\w{5}$"), timeout=10000)
        code = phone.inner_text("#handoff-code")
        dm = lambda: [m["text"] for m in fe.store.threads("dm", 50, None)]  # noqa: E731
        wait_for(lambda: any("made a code to add a computer" in t for t in dm()), 10)
        assert not any(code in t or code.replace("-", "") in t for t in dm())  # never the code itself

        ctx, page, errors, _, loads = _computer(browser, url, code)
        contexts.append(ctx)
        assert len(loads) == 1
        new = next(d for d in fe.relay.devices() if d["id"] != own["id"])
        assert (new["pending"], new["origin"], new["by"]) == (True, "hosted", own["id"])
        expect(phone.locator("#handoff-ask")).to_contain_text(f"{new['code'][:3]} {new['code'][3:]}", timeout=10000)
        mine = phone.locator(f"#phone-devices li[data-device='{own['id']}']")
        expect(mine.locator("button", has_text="Remove")).to_have_count(0)  # never itself
        phone.click("#handoff-approve")
        wait_for(lambda: not fe.relay.store.device_by_id(new["id"]).get("pending"), 10)
        wait_for(lambda: any("approved the computer" in t and new["id"] in t for t in dm()), 10)
        _runs_the_bots_release(page, fe, new["id"])
        assert len(loads) == 2, loads  # the reload after the first adoption wrote the headers
        assert not errors, errors
        assert not phone_errors, phone_errors
    finally:
        for c in contexts:
            c.close()
        site.shutdown()
