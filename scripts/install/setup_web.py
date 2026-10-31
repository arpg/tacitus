#!/usr/bin/env python3
"""The first-run setup page: the install questions in a browser.

docs/setup-web.md is the design. The page asks the questions of
setup_questions.py (the ones `user_init.py` asks at the terminal), checks
a posted phase with the same walk and writes it with the same writer,
then runs the doctor and lists the steps that stay on the terminal.

    python3 scripts/install/setup_web.py                        # loopback
    python3 scripts/install/setup_web.py --bind 100.101.102.103  # the tailnet
        [--port 8480] [--host NAME] [--ea-dir DIR] [--idle-minutes 60]

It prints one address with a one-time token after `#t=`. Every request
must name this server in Host; every /api/ request must carry the token
in X-Setup-Token, and every POST this page's Origin and a JSON body. The
HTTP layer is the web chat's (botcore/botcore/chat/web_frontend.py): a
threading server with a cap on connections, a socket timeout, a body cap
and a strict content policy. Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import http.server
import io
import ipaddress
import json
import os
import pwd
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PAGE_DIR = HERE / "setup_web"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "preprocessor"))
import owner_trust  # noqa: E402
import setup_questions as Q  # noqa: E402
import user_init  # noqa: E402

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8480
TOKEN_HEADER = "X-Setup-Token"
MAX_BODY = 64 << 10
MAX_KEY = 64
MAX_CONNECTIONS = 16
HANDLER_TIMEOUT_S = 30
DOCTOR_TIMEOUT_S = 120
IDLE_S = 3600
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]", "::1")
# The page carries a client secret and chat tokens over plain HTTP, so it
# is served on loopback or on the tailnet (WireGuard) only.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/setup.css": ("setup.css", "text/css; charset=utf-8"),
}
_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$")
_LENGTH = re.compile(r"[0-9]{1,12}")
# How user_init's writers report a file they wrote.
_SAVED = re.compile(r"^-> (?:Saved|Wrote) (\S+)")


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def bind_address(value: str) -> str:
    """The address to listen on: a loopback or a Tailscale address, else
    ValueError with the reason."""
    value = (value or "").strip()
    if value == "localhost":
        value = DEFAULT_BIND
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        raise ValueError(f"--bind takes an IP address, not {value!r}") from None
    if ip.is_loopback or any(ip in net for net in TAILNET):
        return str(ip)
    raise ValueError(f"--bind {value}: only a loopback address or this machine's Tailscale "
                     "address (100.64.0.0/10, fd7a:115c:a1e0::/48); the page carries secrets "
                     "over plain HTTP, so it is served only on a private network")


def _hostport(value: str) -> tuple[str, int | None] | None:
    """(host lowercased, port or None) of a Host header; None when it is
    not a bare host[:port]."""
    value = (value or "").strip()
    if not value or any(c in value for c in "/?#@\\ \t"):
        return None
    try:
        parts = urllib.parse.urlsplit("//" + value)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    return (host, port) if host else None


def next_steps(ctx: dict, platform: str = sys.platform, env: dict | None = None) -> list[dict]:
    """The steps the page leaves to the terminal, in order, each with its
    commands as the user types them (the /setup skill's sections 1, 3 to 5,
    6 and 8)."""
    profile = ctx.get("profile") or "assistant"
    mac = platform == "darwin"
    home = "/Users/ea-bot" if mac else "/home/ea-bot"
    method = (env or {}).get("GRAPH_AUTH_METHOD", "")
    steps = [
        {"title": "In the checkout",
         "text": "Run each command below from the checkout, in a terminal on this machine.",
         "commands": [f"cd {REPO}"]},
        {"title": "Packages, templates and the cron lines",
         "text": "Installs the Python packages, copies the config templates this page did not "
                 "write and saves the cron lines without installing them.",
         "commands": [f"./scripts/install/setup.sh --profile {profile}"]},
    ]
    if method == "confidential":
        steps.append({"title": "Outlook sign-in",
                      "text": "Prints a link, waits for the browser's redirect on localhost:8400 "
                              "and aligns the scopes to what the app grants.",
                      "commands": ["python3 preprocessor/graph_client.py --align --write"]})
    elif method in ("device_code", "interactive"):
        steps.append({"title": "Outlook sign-in",
                      "text": "Prints a link and a code for any browser.",
                      "commands": ["python3 preprocessor/graph_client.py"]})
    else:
        steps.append({"title": "Outlook sign-in",
                      "text": "No app registration yet: docs/it-request.md asks IT for one, and "
                              "docs/SETUP.md 'Option C: no registration (outlook-web-auth)' is "
                              "the other path. Then answer the Outlook screen again.",
                      "commands": []})
    steps.append({"title": "Google sign-in (optional)",
                  "text": "After docs/SETUP.md 'Google OAuth app', with the client secret saved "
                          "as ~/.ea/auth/google_client_secret.json. It asks you to paste the "
                          "address your browser lands on.",
                  "commands": ["python3 preprocessor/google_calendar_client.py --setup",
                               "python3 preprocessor/google_calendar_client.py --test"]})
    if profile != "review-only":
        steps.append({"title": "Your writing style",
                      "text": "Reads 90 days of sent mail, once Outlook is signed in.",
                      "commands": ["python3 scripts/install/user_init.py --style-only"]})
    steps.append({"title": "The service account",
                  "text": "Creates ea-bot, moves ~/.ea into its home and installs its own Claude "
                          "Code and the sudoers rule. After it, this page can no longer write "
                          "the config.",
                  "commands": ["sudo ./scripts/install/setup-ea-user-macos.sh" if mac
                               else "sudo ./scripts/install/setup-ea-user.sh"]})
    if mac:
        steps.append({"title": "ea-bot's Claude login",
                      "text": "ea-bot has no keychain: the token goes in ea-bot's settings "
                              "(docs/macos-claude-binary.md).",
                      "commands": [f"cd / && sudo -u ea-bot env HOME={home} "
                                   f"{home}/.local/bin/claude setup-token"]})
    else:
        steps.append({"title": "ea-bot's Claude login",
                      "text": "Then type /login, and Ctrl+C when it is done.",
                      "commands": [f"sudo -u ea-bot env HOME={home} claude"]})
    py = "/opt/homebrew/bin/python3" if mac else "/usr/bin/python3"
    steps.append({"title": "The install overlay",
                  "text": "Carries your institution's facts into every Claude session. Run both "
                          "again after a change to user-profile.json.",
                  "commands": [f"sudo -u ea-bot {py} {home}/ea-system/preprocessor/install_overlay.py render",
                               f"python3 preprocessor/install_overlay.py install --tree {home}/ea-system"]})
    if profile == "assistant":
        steps.append({"title": "The chat platform",
                      "text": "docs/SETUP.md 'Zulip server' (recommended), or "
                              "docs/SETUP.md 'Slack'. For Zulip, this prints the chat block "
                              "and the key line.",
                      "commands": ["python3 scripts/zulip_bootstrap.py --site <site> "
                                   "--owner-email <you> --owner-key <key>"]})
    steps.append({"title": "Hardening and services",
                  "text": "The owner-owned code tree, the owner-private files and the services.",
                  "commands": (["sudo bash deploy/macos/install-services.sh",
                                "sudo bash deploy/enable-mail-mediation.sh"] if mac
                               else ["sudo bash deploy/harden-permissions.sh"])})
    steps.append({"title": "The cron lines",
                  "text": "Installs them, pointed at the deployed tree.",
                  "commands": [f"./scripts/install/setup.sh --profile {profile} --install-cron"]})
    steps.append({"title": "The checklist and a first briefing",
                  "text": "Fix each FAIL with the fix printed beside it.",
                  "commands": [f"python3 scripts/ea-doctor.py --profile {profile}",
                               "./run-agent.sh morning-briefing --now"]})
    steps.append({"title": "The first-week questions",
                  "text": "Answer each in your own words: python3 preprocessor/onboarding.py "
                          "answer --id <id> --text \"<your words>\".",
                  "commands": ["python3 preprocessor/onboarding.py ask --count 3"]})
    return steps


# -- the server -----------------------------------------------------------

class SetupServer(http.server.ThreadingHTTPServer):
    """One thread per connection, at most MAX_CONNECTIONS at once; the
    state of one setup session: its EA directory, its token, the inbox
    phase's result for the autonomy phase that follows it."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind: str, port: int, *, ea_dir, token: str, hosts=(),
                 doctor_argv: list[str] | None = None, idle_s: float = IDLE_S,
                 tz: str | None = None) -> None:
        self.address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        super().__init__((bind, port), Handler)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.bind = bind
        self.port = self.server_address[1]
        self.ea_dir = Path(ea_dir)
        self.token = token
        ip = ipaddress.ip_address(bind)
        shown = f"[{bind}]" if ip.version == 6 else bind
        self.hosts = {shown.lower(), *(h.lower() for h in hosts)}
        if ip.is_loopback:
            self.hosts.update(LOOPBACK_NAMES)
        self.url = f"http://{shown}:{self.port}/#t={token}"
        self.doctor_argv = list(doctor_argv or [sys.executable, str(REPO / "scripts" / "ea-doctor.py"),
                                                "--json"])
        self.lock = threading.Lock()
        self.doctor_lock = threading.Lock()
        self.inbox: dict | None = None
        self.idle_s = idle_s
        self.last_seen = time.monotonic()
        self.tz = tz

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def context(self) -> dict:
        """The questions' facts for this EA directory, read now. Call with
        `lock` held: user_init's writers read module globals."""
        user_init.use_ea_dir(self.ea_dir)
        if self.tz is None:
            self.tz = user_init.detect_system_timezone()
        return user_init.context(tz=self.tz)

    def stop_later(self) -> None:
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TacitusSetup"
    sys_version = ""
    timeout = HANDLER_TIMEOUT_S
    server: SetupServer

    def log_message(self, fmt: str, *args) -> None:
        pass  # the token rides in a header and the fragment; nothing to log

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    # responses

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))),
                     ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                     ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
                     ("Cross-Origin-Resource-Policy", "same-origin"),
                     ("Content-Security-Policy", CSP)):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    # routing

    def _route(self, method: str) -> None:
        self._body_read = False
        try:
            path = urllib.parse.urlsplit(self.path).path
            self._check_host()
            if path.startswith("/api/"):
                self._check_site()
                self._check_token()
                self.server.last_seen = time.monotonic()
                body = None
                if method == "POST":
                    self._check_origin()
                    body = self._read_json()
                self._api(method, path[len("/api/"):], body)
            elif method == "GET" and path in STATIC:
                name, ctype = STATIC[path]
                try:
                    data = (PAGE_DIR / name).read_bytes()
                except OSError:
                    raise HttpError(404, "not found") from None
                self._send(200, data, ctype)
            else:
                raise HttpError(404, "not found")
        except HttpError as e:
            self._drain()
            self._json(e.status, {"error": e.message, **e.extra})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            with contextlib.suppress(OSError):
                self._json(500, {"error": "internal error; the terminal shows the traceback"})
        self.close_connection = True

    # checks and inputs

    def _check_host(self) -> None:
        got = _hostport(self.headers.get("Host") or "")
        if got is None:
            raise HttpError(421, "this server does not answer for that host")
        name, port = got
        name = f"[{name}]" if ":" in name else name
        if name not in self.server.hosts or (port or 80) != self.server.port:
            raise HttpError(421, "this server does not answer for that host")

    def _check_site(self) -> None:
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            raise HttpError(403, "not from this page")

    def _check_token(self) -> None:
        got = self.headers.get(TOKEN_HEADER) or ""
        if not got or not hmac.compare_digest(got.encode("utf-8"), self.server.token.encode("utf-8")):
            raise HttpError(401, "open the address the terminal printed, with its #t= token")

    def _check_origin(self) -> None:
        """A POST carries the Origin of the address the browser used, which
        _check_host has accepted."""
        try:
            parts = urllib.parse.urlsplit((self.headers.get("Origin") or "").strip())
            port = parts.port
        except ValueError:
            raise HttpError(403, "a POST must carry this page's Origin") from None
        want = _hostport(self.headers.get("Host") or "")
        if parts.scheme != "http" or want is None or (parts.hostname or "").lower() != want[0] \
                or (port or 80) != (want[1] or 80) or parts.path not in ("", "/"):
            raise HttpError(403, "a POST must carry this page's Origin")

    def _drain(self) -> None:
        if self._body_read:
            return
        self._body_read = True
        length = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") or not _LENGTH.fullmatch(length):
            return
        if 0 < int(length) <= MAX_BODY:
            with contextlib.suppress(OSError):
                self.rfile.read(int(length))

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "the body must be JSON")
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "Content-Length is required")
        length = (self.headers.get("Content-Length") or "").strip()
        if not _LENGTH.fullmatch(length):
            raise HttpError(411, "Content-Length is required")
        if int(length) > MAX_BODY:
            raise HttpError(413, f"the body is over {MAX_BODY} bytes")
        self._body_read = True
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
        except ValueError:
            raise HttpError(400, "the body is not JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "the body must be a JSON object")
        return body

    # the API

    def _api(self, method: str, route: str, body: dict | None) -> None:
        srv = self.server
        if method == "GET" and route == "phases":
            with srv.lock:
                ctx = srv.context()
                # The Outlook method picks the sign-in step; the file is
                # the resident's later, so only that key is read.
                env = owner_trust.read_env_values(srv.ea_dir / ".env", ("GRAPH_AUTH_METHOD",))
                self._json(200, {"phases": Q.describe(ctx), "context": self._public(ctx),
                                 "finish": next_steps(ctx, env=env)})
            return
        if method == "GET" and route == "answers":
            with srv.lock:
                ctx = srv.context()
                self._json(200, {"values": Q.current(ctx), "context": self._public(ctx)})
            return
        if method == "POST" and route == "answers":
            self._post_answers(body or {})
            return
        if method == "POST" and route == "doctor":
            self._doctor()
            return
        if method == "POST" and route == "done":
            self._json(200, {"ok": True, "message": "The setup page has stopped."})
            print("setup_web: Done pressed; the setup page stops.", flush=True)
            srv.stop_later()
            return
        raise HttpError(404, "not found")

    def _public(self, ctx: dict) -> dict:
        return {"profile": ctx["profile"], "recorded_profile": ctx["recorded_profile"],
                "chat": ctx["chat"], "acknowledged_on": ctx["acknowledged_on"],
                "ea_dir": str(self.server.ea_dir), "platform": sys.platform}

    @staticmethod
    def _answers(pid: str, raw) -> dict:
        """The posted answers, in the shapes an answers file has, for
        questions this phase asks the page."""
        if not isinstance(raw, dict):
            raise HttpError(400, "answers must be a JSON object")
        known = {q["key"] for q in Q.questions(pid, "web")} if pid != "state" else set()
        for key, value in raw.items():
            if not isinstance(key, str) or len(key) > MAX_KEY or key not in known:
                raise HttpError(400, f"{str(key)[:MAX_KEY]!r} is not a question of {pid}")
            items = value if isinstance(value, list) else [value]
            if isinstance(value, list) and len(value) > Q.MAX_ITEMS:
                raise HttpError(400, f"{key}: at most {Q.MAX_ITEMS} items")
            for item in items:
                if item is not None and not isinstance(item, (str, bool, int)) \
                        or isinstance(item, str) and len(item) > Q.MAX_TEXT:
                    raise HttpError(400, f"{key}: not an answer")
        return raw

    def _post_answers(self, body: dict) -> None:
        srv = self.server
        pid = body.get("phase")
        if pid not in Q.web_phases() and pid != "state":
            raise HttpError(400, "phase must be one of: " + ", ".join(Q.web_phases() + ["state"]))
        raw = self._answers(pid, body.get("answers") or {})
        with srv.lock:
            ctx = srv.context()
            values, errors = (Q.resolve(pid, raw, ctx, "web") if pid != "state" else ({}, {}))
            if errors:
                raise HttpError(422, "Some answers need a change.", errors=errors)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    result = user_init.apply_phase(pid, values, ctx,
                                                   srv.inbox if pid == "autonomy" else None)
            except user_init.AnswerError as e:
                raise HttpError(422, "Some answers need a change.", errors=e.errors) from None
            except PermissionError as e:
                raise HttpError(409, f"Could not write {e.filename}: permission denied. After "
                                     "the service account exists, ~/.ea belongs to ea-bot: change "
                                     "the config there with user_init.py --answers, as ea-bot "
                                     "(docs/install-testing.md).") from None
            except OSError as e:
                raise HttpError(409, f"Could not write: {e}") from None
            except SystemExit as e:
                raise HttpError(422, str(e.code)) from None
            finally:
                sys.stdout.write(out.getvalue())
                sys.stdout.flush()
            if pid == "inbox":
                srv.inbox = result
        lines = [ln.strip() for ln in out.getvalue().splitlines() if ln.strip()]
        saved = [m.group(1) for m in map(_SAVED.match, lines) if m]
        self._json(200, {"ok": True, "saved": saved,
                         "notes": [ln for ln in lines if not _SAVED.match(ln)]})

    def _doctor(self) -> None:
        srv = self.server
        if not srv.doctor_lock.acquire(blocking=False):
            raise HttpError(409, "the checklist is already running")
        try:
            with srv.lock:
                profile = srv.context()["profile"]
            argv = [*srv.doctor_argv, "--ea-dir", str(srv.ea_dir), "--profile", profile]
            env = {**os.environ, "EA_DIR": str(srv.ea_dir)}
            try:
                r = subprocess.run(argv, capture_output=True, text=True, env=env,
                                   stdin=subprocess.DEVNULL, timeout=DOCTOR_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise HttpError(504, f"the checklist did not finish in {DOCTOR_TIMEOUT_S} s") from None
            try:
                doc = json.loads(r.stdout)
                rows = [{k: str(row.get(k) or "") for k in ("status", "name", "detail", "fix")}
                        for row in doc["results"]]
            except (ValueError, KeyError, TypeError, AttributeError):
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
                raise HttpError(502, f"the checklist did not answer: {tail[0][:300]}") from None
        finally:
            srv.doctor_lock.release()
        count = {s: sum(1 for row in rows if row["status"] == s) for s in ("PASS", "WARN", "FAIL")}
        self._json(200, {"results": rows, "pass": count["PASS"], "warn": count["WARN"],
                         "fail": count["FAIL"], "exit": r.returncode})


# -- running it -----------------------------------------------------------

def make_server(ea_dir, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT, *,
                token: str | None = None, hosts=(), doctor_argv=None,
                idle_s: float = IDLE_S, tz: str | None = None) -> SetupServer:
    return SetupServer(bind_address(bind), port, ea_dir=ea_dir,
                       token=token or secrets.token_urlsafe(32), hosts=hosts,
                       doctor_argv=doctor_argv, idle_s=idle_s, tz=tz)


def serve(server: SetupServer) -> None:
    """Serve until Done, the idle limit or Ctrl+C."""
    def watch() -> None:
        while True:
            time.sleep(min(30.0, max(server.idle_s / 4, 0.05)))
            if time.monotonic() - server.last_seen > server.idle_s:
                print(f"setup_web: no request for {server.idle_s / 60:g} minutes; stopping.",
                      flush=True)
                server.shutdown()
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nsetup_web: stopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="The install questions in a browser (docs/setup-web.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--bind", default=DEFAULT_BIND,
                    help="a loopback address (default) or this machine's Tailscale address")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", action="append", default=[], metavar="NAME",
                    help="another name the browser may use for this machine, such as its "
                         "MagicDNS name (repeatable)")
    ap.add_argument("--ea-dir", default=os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    ap.add_argument("--idle-minutes", type=float, default=IDLE_S / 60,
                    help="stop after this long with no request (default 60)")
    a = ap.parse_args(argv)
    if os.geteuid() == 0:
        ap.error("run it as yourself, not as root: it writes your ~/.ea and ~/.ea-private")
    if pwd.getpwuid(os.geteuid()).pw_name == "ea-bot":
        ap.error("run it as the owner, not as ea-bot: the secrets go to the owner's ~/.ea-private")
    for name in a.host:
        if not _HOST_NAME.match(name.lower()):
            ap.error(f"--host {name!r} is not a host name")
    try:
        server = make_server(a.ea_dir, a.bind, a.port, hosts=a.host, idle_s=a.idle_minutes * 60)
    except ValueError as e:
        ap.error(str(e))
    except OSError as e:
        ap.error(f"cannot listen on {a.bind} port {a.port}: {e}")
    print("Tacitus setup: the install questions in a browser.")
    print(f"  EA directory: {server.ea_dir}")
    print("Open this address (the part after #t= is this run's one-time token):")
    print(f"  {server.url}")
    for name in a.host:
        print(f"  or http://{name}:{server.port}/#t={server.token}")
    print(f"It stops when you press Done on the page, after {a.idle_minutes:g} minutes with no "
          "request, or on Ctrl+C.", flush=True)
    serve(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
