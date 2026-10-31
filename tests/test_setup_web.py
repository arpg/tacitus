"""scripts/install/setup_web.py: the first-run setup page (docs/setup-web.md).

The phases it serves are the questions of setup_questions.py, the same
ones user_init.py asks; a posted phase writes byte for byte what
`user_init.py --answers FILE --phase <p>` writes; a bad answer comes back
under its key and writes nothing; the token, Host, Origin and size gates
hold; the doctor route returns the checklist; Done and the idle limit
stop the server; it binds only loopback or tailnet addresses; the page's
files load only themselves and draw text as text. Where Playwright and
Chromium are installed, the page runs in a phone-sized browser from the
profile screen to the checklist.
"""
import http.client
import json
import re
import stat
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install"
PAGE = INSTALL / "setup_web"
sys.path.insert(0, str(INSTALL))
sys.path.insert(0, str(ROOT / "preprocessor"))

import setup_questions as Q  # noqa: E402
import setup_web  # noqa: E402
import user_init  # noqa: E402

ANSWERS_FILE = ROOT / "tests" / "install" / "answers-email.json"
TOKEN = "made-up-token-" + "x" * 30
DOCTOR_CI = [sys.executable, str(ROOT / "scripts" / "ea-doctor.py"), "--json", "--ci"]
PROFILE_ANSWERS = {"profile": "email", "acknowledged": True}


class Client:
    """Raw HTTP to one server, every header under the test's control."""

    def __init__(self, srv):
        self.srv = srv
        self.port = srv.port

    def call(self, method, path, body=None, *, token=TOKEN, origin=True, host=None,
             headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            h["X-Setup-Token"] = token
        if origin and method == "POST":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        text = r.read()
        conn.close()
        try:
            obj = json.loads(text)
        except ValueError:
            obj = text
        return r.status, obj, {k.lower(): v for k, v in r.getheaders()}

    def post(self, phase, answers):
        return self.call("POST", "/api/answers", {"phase": phase, "answers": answers})[:2]


@pytest.fixture
def start(tmp_path, monkeypatch):
    """start(ea_dir=None, **kw) -> (client, server, thread) for a server on
    a free loopback port; every one is stopped at the end."""
    made = []

    def run(ea_dir=None, **kw):
        ea = Path(ea_dir or tmp_path / "web")
        monkeypatch.setenv("EA_PRIVATE_DIR", str(ea / "private"))
        kw.setdefault("doctor_argv", DOCTOR_CI)
        srv = setup_web.make_server(ea, port=0, token=TOKEN, tz="America/Denver", **kw)
        t = threading.Thread(target=setup_web.serve, args=(srv,), daemon=True)
        t.start()
        made.append((srv, t))
        return Client(srv), srv, t
    yield run
    for srv, t in made:
        if t.is_alive():
            srv.shutdown()
            t.join(5)


def answers_for(pid: str, front: str = "web") -> dict:
    data = {**json.loads(ANSWERS_FILE.read_text()), **PROFILE_ANSWERS}
    keys = {q["key"] for q in Q.questions(pid, front)}
    return {k: v for k, v in data.items() if k in keys}


def files_under(root: Path) -> dict:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file()}


# -- one set of questions -------------------------------------------------

def test_the_phases_json_is_the_definitions(start):
    c, srv, _ = start()
    status, doc, _ = c.call("GET", "/api/phases")
    assert status == 200
    assert [p["id"] for p in doc["phases"]] == Q.web_phases()
    with srv.lock:
        ctx = srv.context()
    assert doc["phases"] == Q.describe(ctx)
    for ph in doc["phases"]:
        want = [q for q in Q.questions(ph["id"], "web")
                if all(Q.holds(c, {}, ctx) for c in
                       ([q["when"]] if isinstance(q.get("when"), dict) else q.get("when") or [])
                       if "ctx" in c)]
        assert [q["key"] for q in ph["questions"]] == [q["key"] for q in want], ph["id"]
        for got, q in zip(ph["questions"], want):
            assert got["kind"] == q["kind"]
            assert got["label"] == (q.get("web_label") or q["label"]).strip()
            assert got["required"] == bool(q.get("required"))
    keys = {q["key"] for ph in doc["phases"] for q in ph["questions"]}
    for terminal_only in ("graph-sign-in-now", "google-calendar-now", "style-analysis-now",
                          "group-add-another", "deadline-add-another"):
        assert terminal_only not in keys, terminal_only
    # every answers-file key the page can act on is a question on it
    data = json.loads(ANSWERS_FILE.read_text())
    terminal_only = {"graph-sign-in-now", "google-calendar-now", "style-analysis-now"}
    page_keys = {q["key"] for p in Q.PHASES if p.get("web") for q in p["questions"]}
    assert {k for k in data if not k.startswith("_")} - terminal_only <= page_keys
    # the notice is the README's paragraph
    notice = [q for q in doc["phases"][0]["questions"] if q["key"] == "acknowledged"][0]["notice"]
    assert notice.startswith("Tacitus is not an ordinary program.") and "no warranty" in notice


def test_user_init_holds_no_copy_of_the_questions():
    """The labels live in setup_questions.py only: user_init.py asks them
    from there."""
    source = (INSTALL / "user_init.py").read_text()
    for ph in Q.PHASES:
        for q in ph["questions"]:
            label = q["label"].strip()
            if len(label) >= 12:
                assert label not in source, label
    for task, desc, _tier in Q.TASK_TYPES:
        assert f'"{desc}"' not in source, desc


def test_the_terminal_walks_the_same_questions(monkeypatch, tmp_path):
    """A full `--answers` run looks up exactly the keys the definitions
    walk, phase by phase, with the same conditions."""
    def no_input(p=""):
        raise AssertionError(f"input() called: {p!r}")
    monkeypatch.setattr("builtins.input", no_input)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(sys, "argv", ["user_init.py", "--ea-dir", str(tmp_path),
                                      "--answers", str(ANSWERS_FILE)])
    user_init.main()
    used = set(user_init.ANSWERS_USED)
    raw = json.loads(ANSWERS_FILE.read_text())
    ctx = {"profile": "assistant", "chat": "slack", "ex": {}, "files": {},
           "detected_timezone": "America/Denver"}
    want: set = set()
    for pid in ("identity", "scheduling", "inbox", "autonomy", "groups", "deadlines", "style",
                "auth", "tone", "reimbursement", "vip", "push"):
        _values, errors = Q.resolve(pid, raw, ctx, "terminal", used=want)
        assert not errors, (pid, errors)
    assert used - {"answer-the-first-week-preference-questions-now"} == want


# -- the same files -------------------------------------------------------

def test_posted_answers_write_what_the_terminal_writes(start, monkeypatch, tmp_path):
    """Each phase posted to the page writes the same bytes, with the same
    modes, as `user_init.py --answers <file> --phase <p>` writes from the
    same answers."""
    term, web = tmp_path / "term", tmp_path / "web"
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({**json.loads(ANSWERS_FILE.read_text()), **PROFILE_ANSWERS}))
    phases = ["profile", "identity", "scheduling", "inbox", "autonomy", "groups", "deadlines",
              "style", "auth", "reimbursement", "vip", "push", "state"]
    monkeypatch.setattr("builtins.input", lambda p="": pytest.fail(f"input() called: {p!r}"))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(term / "private"))
    monkeypatch.setattr(user_init, "detect_system_timezone", lambda: "America/Denver")
    for pid in phases:
        monkeypatch.setattr(sys, "argv", ["user_init.py", "--ea-dir", str(term),
                                          "--answers", str(answers), "--phase", pid])
        user_init.main()

    c, _srv, _ = start(web)
    for pid in phases:
        status, doc = c.post(pid, answers_for(pid) if pid != "state" else {})
        assert status == 200, (pid, doc)

    got, want = files_under(web), files_under(term)
    assert sorted(got) == sorted(want)
    for name, path in want.items():
        assert got[name].read_bytes() == path.read_bytes(), name
        assert stat.S_IMODE(got[name].stat().st_mode) == stat.S_IMODE(path.stat().st_mode), name
    assert stat.S_IMODE((web / "private").stat().st_mode) == 0o700
    cfg = json.loads((web / "config" / "ea-config.json").read_text())
    assert cfg["profile"] == "email" and cfg["acknowledged"]["responsibility"]
    assert "mail_mediation" not in cfg and "quiet_hours" in cfg, "started from the template"


def test_lists_post_as_text_as_the_page_sends_them(start, tmp_path):
    c, _srv, _ = start()
    status, doc = c.post("identity", {"full-name": "Pat Example", "work-email": "pat@Uni.Example",
                                      "institution-domains": "Uni.Example, @med.uni.example",
                                      "calendar-accounts": ""})
    assert status == 200, doc
    profile = json.loads((tmp_path / "web" / "config" / "user-profile.json").read_text())
    assert profile["institution"]["domains"] == ["uni.example", "med.uni.example"]
    assert profile["institution"]["safety_rail_domain"] == "uni.example"
    assert profile["institution"]["name"] == "" and profile["calendar_accounts"] == []
    assert doc["saved"] == [str(tmp_path / "web" / "config" / "user-profile.json")]


def test_a_phase_posted_again_keeps_what_later_phases_wrote(start, tmp_path):
    """The page has a Previous button: the scheduling phase posted again
    keeps the reply styles the style phase wrote, and the autonomy phase
    takes archive-as-completion from the inbox phase posted before it."""
    c, _srv, _ = start()
    assert c.post("scheduling", answers_for("scheduling"))[0] == 200
    assert c.post("style", {"style-student": "Brief."})[0] == 200
    assert c.post("scheduling", {"timezone": "America/Chicago"})[0] == 200
    sched = json.loads((tmp_path / "web" / "config" / "scheduling-preferences.json").read_text())
    assert sched["response_style"]["student"] == "Brief." and sched["timezone"] == "America/Chicago"
    assert c.post("inbox", {"inbox-zero": False})[0] == 200
    assert c.post("autonomy", {})[0] == 200
    tiers = json.loads((tmp_path / "web" / "config" / "autonomy-tiers.json").read_text())
    assert tiers["inbox_as_completion"]["enabled"] is False


def test_a_bad_answer_comes_back_under_its_key_and_writes_nothing(start, tmp_path):
    c, _srv, _ = start()
    cfg = tmp_path / "web" / "config"
    status, doc = c.post("identity", {"work-email": "not an address", "video-link": "zoom/x",
                                      "institution-domains": "example.edu, bad domain"})
    assert status == 422
    assert set(doc["errors"]) == {"full-name", "work-email", "video-link", "institution-domains"}
    assert "bad domain" in doc["errors"]["institution-domains"]
    status, doc = c.post("scheduling", {"vip-end-time": "25:00", "default-duration-minutes": "soon",
                                        "timezone": "Mars/Base_One"})
    assert status == 422
    assert set(doc["errors"]) == {"vip-end-time", "default-duration-minutes", "timezone"}
    status, doc = c.post("autonomy", {"conservative-defaults": False,
                                      "tier-draft-email-response": "sometimes"})
    assert status == 422 and list(doc["errors"]) == ["tier-draft-email-response"]
    status, doc = c.post("profile", {"profile": "email", "acknowledged": False})
    assert status == 422 and "accept the notice" in doc["errors"]["acknowledged"]
    status, doc = c.post("auth", {"graph-auth-method": "confidential", "graph-client-id": "id"})
    assert status == 422 and list(doc["errors"]) == ["graph-client-secret"]
    status, doc = c.post("auth", {"graph-auth-method": "device_code", "graph-client-id": "a\nB=c"})
    assert status == 422 and "One line" in doc["errors"]["graph-client-id"]
    # push_notify.problems, the terminal's own check, names the settings
    status, doc = c.post("push", {"push-configure": True, "push-ntfy-url": "http://ntfy.example.org",
                                  "push-topic": "a/b"})
    assert status == 422 and {"push-ntfy-url", "push-topic"} == set(doc["errors"])
    assert not cfg.exists() or not any(cfg.iterdir())
    assert not (tmp_path / "web" / ".env").exists()
    assert not (tmp_path / "web" / "private" / "push.json").exists()
    # a key the phase does not ask, a terminal-only one, a bad shape, a bad phase
    for phase, answers in (("identity", {"timezone": "UTC"}), ("auth", {"graph-sign-in-now": True}),
                           ("identity", {"full-name": {"a": 1}}), ("tone", {}),
                           ("identity", {"full-name": "x" * 3000})):
        assert c.post(phase, answers)[0] == 400, (phase, answers)


def test_secrets_go_owner_private_and_are_never_sent_back(start, tmp_path):
    c, _srv, _ = start()
    assert c.post("profile", {"profile": "assistant", "acknowledged": True})[0] == 200
    status, doc = c.post("auth", {"graph-auth-method": "confidential", "graph-client-id": "cid",
                                  "graph-client-secret": "made-up-secret",
                                  "slack-bot-token": "made-up-bot", "slack-owner-id": "U1"})
    assert status == 200, doc
    secrets = tmp_path / "web" / "private" / "secrets.env"
    assert "GRAPH_CLIENT_SECRET=made-up-secret" in secrets.read_text()
    assert "SLACK_BOT_TOKEN=made-up-bot" in secrets.read_text()
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    env = (tmp_path / "web" / ".env").read_text()
    assert "made-up" not in env and "GRAPH_CLIENT_ID=cid" in env and "EA_SLACK_OWNER_ID=U1" in env
    status, doc, _ = c.call("GET", "/api/answers")
    assert status == 200 and "made-up" not in json.dumps(doc)
    assert doc["values"]["graph-client-secret"] == "" and doc["values"]["graph-auth-method"] == "confidential"
    # the confidential method's sign-in is the step the page leaves
    _s, phases, _ = c.call("GET", "/api/phases")
    commands = [cmd for step in phases["finish"] for cmd in step["commands"]]
    assert "python3 preprocessor/graph_client.py --align --write" in commands
    assert any("zulip_bootstrap.py" in cmd for cmd in commands), "assistant: the chat step"


def test_the_answers_view_is_the_update_view(start, tmp_path):
    """GET /api/answers offers the existing config the way --update does:
    the template's placeholders never, an earlier answer where a default
    names it."""
    cfg = tmp_path / "web" / "config"
    cfg.mkdir(parents=True)
    for name in ("user-profile.json", "scheduling-preferences.json"):
        (cfg / name).write_text((ROOT / "config" / name).read_text())
    c, _srv, _ = start()
    values = c.call("GET", "/api/answers")[1]["values"]
    assert values["full-name"] == "" and values["university"] == ""
    assert values["timezone"] == "America/Denver" and values["profile"] == "email"
    assert values["graph-auth-method"] == "confidential" and values["default-duration-minutes"] == 30
    assert c.post("identity", {"full-name": "Pat Example", "work-email": "pat@uni.example",
                               "university": "Uni Example"})[0] == 200
    values = c.call("GET", "/api/answers")[1]["values"]
    assert values["full-name"] == "Pat Example"
    assert values["institution-name"] == "Uni Example"
    assert values["institution-domains"] == ["uni.example"]
    profile = json.loads((cfg / "user-profile.json").read_text())
    assert "gmail_accounts_note" in profile
    assert profile["institution"]["letterhead"]["template_dir"] == "~/.ea/templates/letterhead"


# -- the gates ------------------------------------------------------------

def test_the_token_host_origin_and_size_gates(start):
    c, srv, _ = start()
    assert c.call("GET", "/api/phases", token=None)[0] == 401
    assert c.call("GET", "/api/answers", token="wrong")[0] == 401
    assert c.call("POST", "/api/answers", {"phase": "state"}, token=None)[0] == 401
    assert c.call("POST", "/api/done", {}, token=None)[0] == 401
    assert c.call("GET", "/api/phases")[0] == 200
    assert c.call("GET", "/api/phases", host="evil.example:%d" % srv.port)[0] == 421
    assert c.call("GET", "/", host="evil.example")[0] == 421
    assert c.call("GET", "/api/phases", host="localhost:%d" % srv.port)[0] == 200
    assert c.call("POST", "/api/doctor", {}, origin=False)[0] == 403
    assert c.call("POST", "/api/doctor", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert c.call("GET", "/api/phases", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert c.call("POST", "/api/answers", raw=b"{}",
                  headers={"Content-Type": "text/plain"})[0] == 415
    big = json.dumps({"phase": "identity", "answers": {"bio-blurb": "x" * (70 << 10)}}).encode()
    # The server refuses the body on its Content-Length before reading it;
    # a client still sending may see the socket reset instead of the 413.
    try:
        assert c.call("POST", "/api/answers", raw=big)[0] == 413
    except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected):
        pass
    assert c.call("POST", "/api/answers", raw=b"[1]")[0] == 400
    assert c.call("GET", "/api/nothing")[0] == 404
    assert c.call("GET", "/../setup_web.py")[0] == 404
    status, page, headers = c.call("GET", "/", token=None)
    assert status == 200 and b"setup.js" in page
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"


def test_the_bind_rule():
    assert setup_web.bind_address("127.0.0.1") == "127.0.0.1"
    assert setup_web.bind_address("localhost") == "127.0.0.1"
    assert setup_web.bind_address("::1") == "::1"
    assert setup_web.bind_address("100.101.102.103") == "100.101.102.103"
    assert setup_web.bind_address("fd7a:115c:a1e0::5") == "fd7a:115c:a1e0::5"
    for bad in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.2", "8.8.8.8", "example.com", ""):
        with pytest.raises(ValueError):
            setup_web.bind_address(bad)
    with pytest.raises(SystemExit):
        setup_web.main(["--bind", "0.0.0.0"])
    with pytest.raises(SystemExit):
        setup_web.main(["--host", "bad host"])


def test_a_name_given_with_host_is_accepted_and_others_are_not(start):
    """Over the tailnet the browser may use the MagicDNS name (--host);
    the printed URL names the bound address."""
    c, srv, _ = start(hosts=["box.made-up-tailnet.ts.net"])
    assert srv.url == f"http://127.0.0.1:{srv.port}/#t={TOKEN}"
    name = f"box.made-up-tailnet.ts.net:{srv.port}"
    assert c.call("GET", "/api/phases", host=name)[0] == 200
    assert c.call("POST", "/api/doctor", {}, host=name,
                  headers={"Origin": f"http://{name}"})[0] == 200
    assert c.call("GET", "/api/phases", host=f"other.made-up-tailnet.ts.net:{srv.port}")[0] == 421


def test_an_ipv6_bind_is_bracketed_in_the_url(tmp_path):
    try:
        srv = setup_web.make_server(tmp_path, "::1", 0, token=TOKEN, tz="UTC")
    except OSError as e:
        pytest.skip(f"no IPv6 loopback here: {e}")
    try:
        assert srv.url == f"http://[::1]:{srv.port}/#t={TOKEN}"
        assert "[::1]" in srv.hosts
    finally:
        srv.server_close()


# -- the doctor, Done and the idle limit ---------------------------------

def test_the_doctor_route_returns_rows(start):
    c, _srv, _ = start()
    assert c.post("profile", PROFILE_ANSWERS)[0] == 200
    status, doc, _ = c.call("POST", "/api/doctor", {})
    assert status == 200, doc
    assert doc["pass"] >= 1 and doc["fail"] == 0
    assert all(set(row) == {"status", "name", "detail", "fix"} for row in doc["results"])
    assert {"name": "profile", "status": "PASS", "detail": "email", "fix": ""} in doc["results"]


def test_a_doctor_that_says_nothing_useful_is_a_502(start):
    c, _srv, _ = start(doctor_argv=[sys.executable, "-c", "print('not json')"])
    status, doc, _ = c.call("POST", "/api/doctor", {})
    assert status == 502 and "not json" in doc["error"]


def test_done_stops_the_server(start):
    c, _srv, t = start()
    status, doc, _ = c.call("POST", "/api/done", {})
    assert status == 200 and doc["ok"]
    t.join(5)
    assert not t.is_alive()


def test_the_idle_limit_stops_the_server(start):
    _c, _srv, t = start(idle_s=0.3)
    t.join(5)
    assert not t.is_alive()


# -- the steps left to the terminal ---------------------------------------

@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("profile", ["email", "review-only", "assistant"])
def test_every_step_names_a_script_that_exists(platform, profile):
    steps = setup_web.next_steps({"profile": profile}, platform,
                                 env={"GRAPH_AUTH_METHOD": "device_code"})
    commands = [cmd for s in steps for cmd in s["commands"]]
    assert f"./scripts/install/setup.sh --profile {profile} --install-cron" in commands
    assert "python3 preprocessor/graph_client.py" in commands
    want = ("sudo ./scripts/install/setup-ea-user-macos.sh" if platform == "darwin"
            else "sudo ./scripts/install/setup-ea-user.sh")
    assert want in commands
    for cmd in commands:
        for path in re.findall(r"(?:\./|/home/ea-bot/ea-system/|/Users/ea-bot/ea-system/)?"
                               r"((?:scripts|preprocessor|deploy)/[\w./-]+\.(?:py|sh))", cmd):
            assert (ROOT / path).is_file(), (cmd, path)
        assert "run-agent.sh" not in cmd or (ROOT / "run-agent.sh").is_file()


# -- the page's files -----------------------------------------------------

class Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_the_page_loads_only_its_own_files_and_draws_text_as_text():
    html = (PAGE / "index.html").read_text()
    js = (PAGE / "setup.js").read_text()
    css = (PAGE / "setup.css").read_text()
    p = Refs()
    p.feed(html)
    assert [a.get("src") for t, a in p.tags if t == "script"] == ["setup.js"]
    assert [a.get("href") for t, a in p.tags if t == "link" and a.get("rel") == "stylesheet"] == ["setup.css"]
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and " style=" not in html and not re.search(r"\son[a-z]+=", html)
    for text in (html, js, css):
        assert not re.search(r"https?://", text)
    assert "@import" not in css and "url(" not in css
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                 "eval(", "new Function", "localStorage"):
        assert word not in js, word
    assert "prefers-color-scheme: dark" in css
    assert set(setup_web.STATIC.values()) == {("index.html", "text/html; charset=utf-8"),
                                              ("setup.js", "text/javascript; charset=utf-8"),
                                              ("setup.css", "text/css; charset=utf-8")}


def test_the_page_stays_small():
    lines = sum(len((PAGE / n).read_text().splitlines()) for n in ("index.html", "setup.js", "setup.css"))
    assert lines <= 700, f"{lines} lines: docs/setup-web.md keeps the page near 600"


# -- in a browser ---------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:  # noqa: BLE001 - no driver on this machine
        pytest.skip(f"playwright cannot start: {e}")
    try:
        b = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001 - no browser installed
        pw.stop()
        pytest.skip(f"no chromium for playwright: {e}")
    yield b
    b.close()
    pw.stop()


def test_the_page_in_a_phone_browser(browser, start, tmp_path):
    from playwright.sync_api import expect
    _c, srv, t = start()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # The two 422s this test provokes are answers, not errors.
    page.on("console", lambda m: m.type == "error" and "status of 422" not in m.text
            and errors.append(m.text))
    page.goto(srv.url)
    expect(page.locator("#title")).to_have_text("Profile")
    assert "#t=" not in page.url, "the token leaves the address bar"
    expect(page.locator(".notice")).to_contain_text("Tacitus is not an ordinary program.")
    page.click("#next")
    expect(page.locator("#f-acknowledged-err")).to_contain_text("accept the notice")
    page.check("input[name=f-acknowledged]")
    page.click("#next")
    expect(page.locator("#title")).to_have_text("You")
    expect(page.locator("#status")).to_contain_text("ea-config.json")
    page.click("#next")
    expect(page.locator("#f-full-name-err")).to_have_text("An answer is required.")
    page.fill("#f-full-name", "Pat Example")
    page.fill("#f-work-email", "pat@uni.example")
    page.click("#next")
    expect(page.locator("#title")).to_have_text("Scheduling")
    expect(page.locator("#f-timezone")).to_have_value("America/Denver")
    page.click("#next")
    expect(page.locator("#title")).to_have_text("Inbox")
    page.uncheck("input[name=f-inbox-zero]")
    expect(page.locator('[data-key="archive-as-completion"]')).to_be_hidden()
    page.check("input[name=f-inbox-zero]")
    expect(page.locator('[data-key="archive-as-completion"]')).to_be_visible()
    for title in ("Autonomy", "Group lists", "Deadline senders", "Reply style",
                  "Outlook and chat"):
        page.click("#next")
        expect(page.locator("#title")).to_have_text(title)
    page.check("input[name=f-graph-auth-method][value=skip]")
    expect(page.locator('[data-key="graph-client-id"]')).to_be_hidden()
    expect(page.locator('[data-key="slack-bot-token"]')).to_have_count(0)
    for title in ("Reimbursement", "VIP senders", "Phone push"):
        page.click("#next")
        expect(page.locator("#title")).to_have_text(title)
    page.click("#prev")
    expect(page.locator("#title")).to_have_text("VIP senders")
    page.click("#next")
    expect(page.locator("#next")).to_have_text("Finish")
    page.click("#next")
    expect(page.locator("#finish h1")).to_be_visible()
    expect(page.locator("#rows li.row.ok").first).to_be_visible(timeout=60000)
    expect(page.locator("#steps")).to_contain_text("./scripts/install/setup.sh --profile email")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(21, 23, 27)"
    assert (tmp_path / "web" / "state" / "obligations-ledger.json").exists()
    profile = json.loads((tmp_path / "web" / "config" / "user-profile.json").read_text())
    assert profile["name"] == "Pat Example" and profile["institution"]["domains"] == ["uni.example"]
    page.click("#next")
    expect(page.locator("#bye")).to_be_visible()
    t.join(5)
    assert not t.is_alive()
    assert not errors, errors
    ctx.close()
