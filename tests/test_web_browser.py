"""Browser boundary and real form flow. No external site is submitted."""
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import web_browser as W


def test_plans_cannot_smuggle_code_or_submission_into_preview():
    for plan in ["{}", '[{"action":"evaluate","selector":"body","value":"fetch(x)"}]',
                 '[{"action":"click","selector":"button"}]',
                 '[{"action":"fill","selector":"input","value":4}]',
                 '[{"action":"fill","selector":"input","label":"x","value":"x"}]',
                 '[{"action":"fill","selector":"input","value":"x","javascript":"x"}]']:
        with pytest.raises(ValueError):
            W.parse_actions(plan, "fill")
    with pytest.raises(ValueError):
        W.parse_actions('[{"action":"check","label":"Yes"}]', "read")


def test_upload_refuses_links_parent_swaps_and_files_outside_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = tmp_path / "private"
    other.mkdir()
    secret = other / "token"
    secret.write_text("private")
    monkeypatch.setattr(W.M, "_allowed_roots", lambda: [allowed])
    normal = allowed / "proposal.txt"
    normal.write_text("proposal")
    assert W.upload_file(str(normal))["buffer"] == b"proposal"
    (allowed / "file-link").symlink_to(secret)
    (allowed / "parent-link").symlink_to(other, target_is_directory=True)
    os.link(secret, allowed / "hard-link")
    for path in [secret, allowed / "file-link", allowed / "parent-link" / "token", allowed / "hard-link"]:
        with pytest.raises((ValueError, OSError)):
            W.upload_file(str(path))


def test_route_blocks_private_redirect_target_before_any_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(W.F.requests, "Session", lambda: calls.append("connect"))
    route = SimpleNamespace(abort=lambda: calls.append("abort"))
    for url in ["http://127.0.0.1/", "http://169.254.169.254/", "http://100.64.0.1/", "file:///etc/passwd"]:
        blocked = []
        W.route_handler(blocked)(route, SimpleNamespace(url=url))
        assert blocked and calls[-1] == "abort"
    assert "connect" not in calls


def test_route_pins_dns_disables_proxy_and_preserves_post(monkeypatch):
    trace = []
    info = {"host": "public.example", "ips": ["8.8.8.8"], "addrinfo": ["pinned"]}
    monkeypatch.setattr(W.F, "validate_url", lambda url: info)

    @contextmanager
    def pinned(host, addresses):
        trace.append((host, addresses))
        yield
        trace.append("unpin")
    monkeypatch.setattr(W.F, "pinned_dns", pinned)

    class Response:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/", "Content-Encoding": "gzip"}
        raw = SimpleNamespace(headers=SimpleNamespace(getlist=lambda key: []))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): return [b"body"]

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, url, **kw):
            assert self.trust_env is False
            assert trace[-1] == ("public.example", ["pinned"])
            assert method == "POST" and kw["data"] == b"answer=yes"
            assert kw["allow_redirects"] is False and "host" not in kw["headers"]
            return Response()
    monkeypatch.setattr(W.F.requests, "Session", Session)
    fulfilled = []
    route = SimpleNamespace(fulfill=lambda **kw: fulfilled.append(kw), abort=lambda: pytest.fail("aborted"))
    request = SimpleNamespace(url="https://public.example", method="POST", post_data_buffer=b"answer=yes",
                              all_headers=lambda: {"host": "attacker", "cookie": "ephemeral=yes"})
    W.route_handler([])(route, request)
    assert fulfilled[0]["status"] == 302
    assert fulfilled[0]["headers"]["Location"] == "http://127.0.0.1/"
    assert "Content-Encoding" not in fulfilled[0]["headers"]
    assert trace[-1] == "unpin"


def test_mediator_runs_browser_as_resident_with_no_owner_environment(tmp_path, monkeypatch):
    import mail_mediator as MM
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setattr(MM.M, "QUEUE", queue)
    monkeypatch.setattr(MM.M, "EA_DIR", tmp_path)
    monkeypatch.setattr(MM, "AUDIT", tmp_path / "audit")
    monkeypatch.setattr(MM, "ASYNC_TOOLS", set())
    monkeypatch.setenv("OWNER_SECRET", "must-not-cross")
    monkeypatch.setenv(W.G.TOKEN_ENV, "must-not-forward-an-approval")
    monkeypatch.setattr(MM.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir="/home/ea-bot"))
    seen = []
    def child(cmd, **kw):
        seen.append((cmd, kw))
        return SimpleNamespace(stdout='{"ok":true}', stderr="", returncode=0)
    monkeypatch.setattr(MM.subprocess, "run", child)
    req = queue / "req-browser.json"
    req.write_text(json.dumps({"tool": "web_browser", "verb": "read",
                               "argv": ["read", "--url", "https://forms.example/form"]}))
    MM.handle(str(req))
    assert len(seen) == 1
    cmd, kw = seen[0]
    assert cmd[:7] == ["sudo", "-n", "-u", "ea-bot", "env", "-i", "HOME=/home/ea-bot"]
    assert kw["env"] == {"PATH": "/usr/bin:/bin"}
    assert "EA_BROWSER_DIRECT=1" in cmd and "must-not-cross" not in str(seen)
    assert "must-not-forward-an-approval" not in str(seen)
    assert json.loads((queue / "resp-browser.json").read_text())["rc"] == 0


def test_mediator_submission_timeout_is_unknown_not_safe_to_retry(tmp_path, monkeypatch):
    import mail_mediator as MM
    import subprocess
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setattr(MM.M, "QUEUE", queue)
    monkeypatch.setattr(MM.M, "EA_DIR", tmp_path)
    monkeypatch.setattr(MM, "AUDIT", tmp_path / "audit")
    monkeypatch.setattr(MM, "ASYNC_TOOLS", set())
    monkeypatch.setattr(MM.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir="/home/ea-bot"))
    calls = []
    def child(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 120)
    monkeypatch.setattr(MM.subprocess, "run", child)
    argv = ["submit", "--url", "https://forms.example/form", "--actions-json", "[]",
            "--submit-selector", "button", "--success-text", "Recorded"]
    req = queue / "req-timeout.json"
    req.write_text(json.dumps({"tool": "web_browser", "verb": "submit", "argv": argv}))
    MM.handle(str(req))
    response = json.loads((queue / "resp-timeout.json").read_text())
    result = json.loads(response["stdout"])
    assert len(calls) == 1 and response["rc"] == 3
    assert result["submitted"] is None and "Status unknown" in result["error"]


FORM = """<!doctype html><html><body>
<form id="f"><label>Name<input name="name" required></label>
<label for="area">Area</label><select id="area" name="area"><option>Choose</option><option>Research</option></select>
<label><input type="checkbox" name="agree">I agree</label>
<button type="button" onclick="document.querySelector('#second').hidden=false">Next</button>
<div id="second" hidden><label>Units<input name="units"></label>
<button type="submit">Submit request</button></div></form>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();
await fetch('/record',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});
document.body.innerHTML='<h1>Response recorded</h1>'};</script>
</body></html>"""


def test_real_browser_inspects_previews_and_verifies_submission(tmp_path, monkeypatch):
    """Actual Chromium, with HTTP transport replaced by an in-memory site.

    All routing, rendering, DOM discovery, fill replay, POST and success
    detection are real. No sockets or external form responses are needed.
    """
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Chromium is not installed; install it to run the browser integration test")
    monkeypatch.setattr(W.F, "EA_DIR", tmp_path)
    monkeypatch.setattr(W, "CONFIRM_TIMEOUT_MS", 1000)
    monkeypatch.setattr(W.F, "validate_url", lambda url: {
        "host": "forms.example", "ips": ["8.8.8.8"], "addrinfo": []})
    responses = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        raw = SimpleNamespace(headers=SimpleNamespace(getlist=lambda key: []))
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): return [self.data]

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, url, **kw):
            if method == "POST":
                responses.append(json.loads(kw["data"]))
                return Response(b"ok")
            return Response(FORM.encode())
    monkeypatch.setattr(W.F.requests, "Session", Session)
    args = SimpleNamespace(verb="read", url="https://forms.example/", actions_json="[]", max_chars=12000)
    result = W.run(args)
    assert result["ok"] and result["submitted"] is False and not responses
    assert any(f["label"] == "Name" for f in result["fields"])
    assert Path(result["screenshot"]).is_file()
    args.verb = "fill"
    args.actions_json = json.dumps([
        {"action": "fill", "label": "Name", "value": "Test Person"},
        {"action": "select", "label": "Area", "value": "Research"},
        {"action": "check", "label": "I agree"},
        {"action": "next", "selector": "button[type=button]"},
        {"action": "fill", "label": "Units", "value": "450000"}])
    # Without an owner-click capability, this prepares a card and must
    # not open a browser, enter answers, upload or submit anything.
    staged = W.run(args)
    assert staged["approval_required"] and not responses
    assert (tmp_path / "state" / "pending-browser.json").is_file()
    # The rest exercises the already-approved worker. Token binding has
    # separate tests, without weakening any production check.
    monkeypatch.setenv(W.G.TOKEN_ENV, "test-approval")
    monkeypatch.setattr(W.G, "require", lambda item: None)
    result = W.run(args)
    assert result["ok"] and not result["submitted"] and not responses, result
    assert any(f["value"] == "450000" for f in result["fields"])
    # A preview cannot click a submit button under the navigation operation.
    original = args.actions_json
    args.actions_json = json.dumps(json.loads(original) + [{"action": "next", "selector": "button[type=submit]"}])
    refused = W.run(args)
    assert not refused["ok"] and "non-submit" in refused["error"] and not responses
    args.actions_json = original
    args.verb = "submit"
    args.submit_selector = "button[type=submit]"
    args.success_text = "Response recorded"
    result = W.run(args)
    assert result["ok"] and result["submitted"] is True and result["submit_clicked"] is True
    assert responses == [{"name": "Test Person", "area": "Research", "agree": "on", "units": "450000"}]
    # An incorrect success marker must not claim failure or retry the POST.
    args.success_text = "Different confirmation"
    result = W.run(args)
    assert not result["ok"] and result["submitted"] is None and result["submit_clicked"] is True
    assert len(responses) == 2 and "Status unknown" in result["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
