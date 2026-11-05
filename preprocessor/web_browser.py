#!/usr/bin/env python3
"""On-demand public web reading and form interaction, as the resident.

Codex calls this through the existing file-queue mediator. The mediator
starts this same program as ea-bot outside the network sandbox, with a
clean environment. It never starts an owner browser or borrows a login.
Each call uses a fresh context. Replay the fill actions to preview or
submit a form; no hidden persistent session or arbitrary JavaScript API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pwd
import stat
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_url as F
import mail_mediation as M
import resident_io as R
import browser_gate as G

MAX_ACTIONS = 80
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CONFIRM_TIMEOUT_MS = 15000

# Fixed inspection code only: page content cannot supply executable code.
INSPECT = r"""() => {
  const visible = e => !!(e.getClientRects().length);
  const controls = [...document.querySelectorAll(
    'input,textarea,select,button,[role="textbox"],[role="radio"],[role="checkbox"],[role="combobox"],[role="button"]'
  )].filter(e => visible(e) || e.type === 'file').slice(0, 250);
  return controls.map((e,i) => {
    e.setAttribute('data-ea-field', String(i));
    const question = e.closest('[data-automation-id="questionItem"],fieldset');
    return {selector: '[data-ea-field="'+i+'"]', tag: e.tagName.toLowerCase(),
      type: e.getAttribute('type'), role: e.getAttribute('role'),
      label: e.getAttribute('aria-label') || [...(e.labels || [])].map(x=>x.innerText).join(' ') ||
        (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>document.getElementById(id)?.innerText || '').join(' ').trim() ||
        e.getAttribute('placeholder') || (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button' ? e.innerText : ''),
      question: question?.innerText.slice(0,1500), required: e.required || e.getAttribute('aria-required') === 'true',
      value: e.type === 'password' ? null : (e.value ?? null), checked: e.checked ?? e.getAttribute('aria-checked'),
      options: e.tagName === 'SELECT' ? [...e.options].map(o=>({label:o.label,value:o.value})) : undefined};
  });
}"""


def parse_actions(raw: str, verb: str) -> list[dict]:
    if len(raw) > 128 * 1024:
        raise ValueError("actions JSON exceeds 128 KiB")
    actions = json.loads(raw)
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError(f"actions must be a list of at most {MAX_ACTIONS} steps")
    if verb == "read" and actions:
        raise ValueError("read does not accept form actions")
    for a in actions:
        if not isinstance(a, dict) or set(a) - {"action", "selector", "label", "value", "path", "sha256"}:
            raise ValueError("unknown action keys")
        if a.get("action") not in {"fill", "check", "select", "upload", "next", "back"}:
            raise ValueError("allowed actions: fill, check, select, upload, next, back")
        targets = [k for k in ("selector", "label") if isinstance(a.get(k), str) and a[k]]
        if len(targets) != 1:
            raise ValueError("each action needs exactly one selector or exact label")
        if a["action"] in {"fill", "select"} and not isinstance(a.get("value"), str):
            raise ValueError("fill/select value must be a string")
        if a["action"] == "check" and not isinstance(a.get("value", True), bool):
            raise ValueError("check value must be boolean")
        if a["action"] == "upload" and not isinstance(a.get("path"), str):
            raise ValueError("upload needs a path")
    return actions


def upload_file(path: str) -> dict:
    """Open an upload only in the existing mediated file roots, no links.

    Walk below the trusted root with directory fds so a swapped parent
    cannot redirect the read to credentials. The whole read is ea-bot's.
    """
    target = Path(os.path.abspath(path))
    for root in M._allowed_roots():
        root = root.resolve()
        try:
            parts = target.relative_to(root).parts
        except ValueError:
            continue
        if not parts:
            continue
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_UPLOAD_BYTES:
                    raise ValueError("upload must be a single-linked regular file, at most 20 MiB")
                data = f.read(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("upload exceeds 20 MiB")
            return {"name": target.name, "mimeType": mimetypes.guess_type(target.name)[0] or
                    "application/octet-stream", "buffer": data}
        finally:
            os.close(fd)
    raise ValueError("upload path is outside the EA's allowed file roots")


def route_handler(blocked: list, policy=None):
    """Validate AND pin every HTTP request, including redirects and POSTs.

    Chromium receives each response through route.fulfill. It never gets a
    second, unvalidated DNS lookup. Requests has no environment proxy or
    netrc credentials. Redirects are fulfilled one hop at a time, causing
    the browser's next hop to pass through this check too.
    """
    def handle(route, request):
        try:
            if policy and policy.get("entered_data"):
                if G.origin(request.url) not in policy["origins"]:
                    raise ValueError("data-bearing browser request leaves the approved HTTPS origins")
            info = F.validate_url(request.url)
            headers = {k: v for k, v in request.all_headers().items()
                       if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
            with F.requests.Session() as session:
                session.trust_env = False
                with F.pinned_dns(info["host"], info["addrinfo"]):
                    with session.request(request.method, request.url, headers=headers,
                                         data=request.post_data_buffer, allow_redirects=False,
                                         timeout=15, stream=True) as response:
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise ValueError("browser response exceeds 8 MiB")
                            chunks.append(chunk)
                        out_headers = {k: v for k, v in response.headers.items()
                                       if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                        # Preserve separate Set-Cookie lines (not comma-joined).
                        cookies = response.raw.headers.getlist("Set-Cookie")
                        if cookies:
                            out_headers["Set-Cookie"] = "\n".join(cookies)
                        route.fulfill(status=response.status_code, headers=out_headers, body=b"".join(chunks))
                        F._log_event("fetch", url=request.url, ip=info["ips"][0],
                                     method="web-browser", http_method=request.method, status=response.status_code)
        except Exception as e:
            blocked.append({"url": request.url, "error": str(e)[:400]})
            F._log_event("blocked", url=request.url, method="web-browser", reason=str(e))
            route.abort()
    return handle


def settle(page):
    # Many forms keep a telemetry connection open. A bounded network-idle
    # wait lets rendering finish without waiting indefinitely on analytics.
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    # JS form hosts initially render only a loading shell. Network-idle
    # alone can time out while their bundles are still loading. Do not
    # report that shell as a successfully inspected form.
    page.wait_for_function(r"""() => {
      const text = document.body?.innerText.trim() || '';
      return text.length > 0 && !/^(loading[.\s…]*|please wait[.\s…]*)$/i.test(text);
    }""", timeout=30000)


def save_screenshot(path: Path, data: bytes):
    # Use the shared confined writer even though production refuses to
    # run as the owner. Imports and future callers retain the same rule.
    if not R.make_dir(path.parent) or not R.replace_bytes(path, data):
        raise OSError("could not save the browser screenshot")


def stage_plan(path: Path, item: dict):
    if not R.make_dir(path.parent) or not R.replace_text(path, json.dumps({"item": item})):
        raise OSError("could not stage the form approval")


def snapshot(page, max_chars: int) -> dict:
    fields = page.evaluate(INSPECT)
    text = page.locator("body").inner_text(timeout=10000)
    folder = F.EA_DIR / "state" / "browser"
    shot = folder / f"{uuid.uuid4().hex}.png"
    # Bytes are written as the resident, never the mediator/owner.
    data = page.screenshot(full_page=False)
    save_screenshot(shot, data)
    host = (F.urlsplit(page.url).hostname or "").lower()
    needs_login = host in {"login.microsoftonline.com", "login.live.com", "accounts.google.com"}
    return {"url": page.url, "title": page.title(), "content": text[:max_chars],
            "authentication_required": needs_login,
            "truncated": len(text) > max_chars, "fields": fields, "screenshot": str(shot)}


def act(page, action: dict, uploads=None):
    loc = (page.locator(action["selector"]) if "selector" in action
           else page.get_by_label(action["label"], exact=True))
    kind = action["action"]
    if kind == "fill":
        loc.fill(action["value"])
    elif kind == "check":
        want = action.get("value", True)
        role = loc.get_attribute("role")
        if role in {"radio", "checkbox"} and loc.evaluate("e => e.tagName.toLowerCase()") != "input":
            if (loc.get_attribute("aria-checked") == "true") != want:
                loc.click()
            if (loc.get_attribute("aria-checked") == "true") != want:
                raise ValueError("the custom checkbox/radio did not keep its requested state")
        else:
            loc.set_checked(want)
    elif kind == "select":
        if loc.evaluate("e => e.tagName.toLowerCase()") == "select":
            loc.select_option(label=action["value"])
        elif loc.get_attribute("role") in {"combobox", "listbox"}:
            loc.click()
            page.get_by_role("option", name=action["value"], exact=True).click()
        else:
            raise ValueError("select needs a select, combobox or listbox field")
    elif kind == "upload":
        loc.set_input_files(uploads[action["path"]])
    else:
        # Only explicit Next/Back buttons for multi-page forms, no general
        # click or evaluate primitive hidden in a preview request.
        name = loc.inner_text().strip().lower()
        tag = loc.evaluate("e => [e.tagName.toLowerCase(), e.getAttribute('type'), e.getAttribute('role')]")
        allowed = {"next", "next page"} if kind == "next" else {"back", "previous", "previous page"}
        if name not in allowed or tag[1] == "submit" or not (tag[0] == "button" or tag[2] == "button"):
            raise ValueError("navigation action must target a non-submit Next/Back button")
        loc.click()
        settle(page)
        page.evaluate(INSPECT)  # refresh selectors on the next page


def run(args) -> dict:
    actions = parse_actions(args.actions_json, args.verb)
    if not 1 <= args.max_chars <= 30000:
        raise ValueError("max-chars must be between 1 and 30000")
    F.validate_url(args.url)
    uploads = {}
    if args.verb != "read":
        G.origin(args.url)  # no plain HTTP when answers or files are involved
        for a in actions:
            if a["action"] == "upload":
                payload = upload_file(a["path"])
                sha = hashlib.sha256(payload["buffer"]).hexdigest()
                if a.get("sha256") and a["sha256"] != sha:
                    raise ValueError("upload changed after review; stage the form again")
                a["sha256"] = sha
                uploads[a["path"]] = payload
        item = G.validate({"verb": args.verb, "url": args.url, "actions": actions,
                           "origins": json.loads(getattr(args, "origins_json", "[]")) or [G.origin(args.url)],
                           "submit_selector": getattr(args, "submit_selector", ""),
                           "success_text": getattr(args, "success_text", "")})
        G.render(item)  # refuse a plan too large or misleading to review
        if not os.environ.get(G.TOKEN_ENV):
            path = F.EA_DIR / "state" / G.STAGE_FILE
            stage_plan(path, item)
            return {"ok": True, "approval_required": True, "submitted": False,
                    "message": "Form plan staged for the owner's confirmation. No answers entered or uploaded."}
        G.require(item)
    policy = {"entered_data": False, "origins": item["origins"] if args.verb != "read" else []}
    from playwright.sync_api import sync_playwright
    blocked = []
    result = {"ok": False, "submitted": False, "submit_clicked": False}
    with sync_playwright() as p:
        # Chromium itself has no working proxy. Only the Python route
        # broker performs HTTP, with pinned DNS and the origin policy.
        # QUIC and direct WebRTC UDP must not bypass HTTP interception.
        browser = p.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"},
            args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                  "--proxy-bypass-list=<-loopback>"])
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False,
                                          user_agent=F.USER_AGENT)
            context.route("**/*", route_handler(blocked, policy))
            # HTTP routing does not intercept WebSockets. Refuse them.
            context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_default_timeout(15000)
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            settle(page)
            page.evaluate(INSPECT)
            if args.verb != "read":
                if G.origin(page.url) not in item["origins"]:
                    raise ValueError("form redirected outside the approved origins; read and stage its final URL")
                policy["entered_data"] = True
            for index, action in enumerate(actions):
                act(page, action, uploads)
                result["completed_actions"] = index + 1
            if args.verb == "submit":
                if not args.submit_selector or not args.success_text:
                    raise ValueError("submit requires --submit-selector and --success-text from the form")
                success = page.get_by_text(args.success_text, exact=False)
                if success.count() and success.first.is_visible():
                    raise ValueError("success text is already visible before submission; choose a confirmation unique to completion")
                # Once clicked, a timeout is ambiguous. Never report False
                # or automatically retry a potentially recorded response.
                result.update(submit_clicked=True, submitted=None)
                page.locator(args.submit_selector).click(no_wait_after=True)
                try:
                    success.first.wait_for(state="visible", timeout=CONFIRM_TIMEOUT_MS)
                    result["submitted"] = True
                except Exception:
                    result["error"] = "Submission confirmation was not observed. Status unknown; inspect before any retry."
            result.update(snapshot(page, args.max_chars))
            result["ok"] = args.verb != "submit" or result["submitted"] is True
        except Exception as e:
            result["error"] = str(e)[:1000]
            if "page" in locals():
                try:
                    result.update(snapshot(page, args.max_chars))
                except Exception:
                    pass
        finally:
            browser.close()
    result["blocked_requests"] = blocked[:15]
    F._log_event("browser-action", url=args.url, verb=args.verb,
                 actions=len(actions), submitted=result["submitted"], ok=result["ok"])
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subs = parser.add_subparsers(dest="verb", required=True)
    read = subs.add_parser("read", allow_abbrev=False)
    read.add_argument("--url", required=True)
    read.add_argument("--max-chars", type=int, default=12000)
    read.set_defaults(actions_json="[]")
    fill = subs.add_parser("fill", allow_abbrev=False)
    fill.add_argument("--url", required=True)
    fill.add_argument("--actions-json", required=True)
    fill.add_argument("--max-chars", type=int, default=12000)
    fill.add_argument("--origins-json", default="[]")
    submit = subs.add_parser("submit", allow_abbrev=False)
    submit.add_argument("--url", required=True)
    submit.add_argument("--actions-json", required=True)
    submit.add_argument("--submit-selector", required=True)
    submit.add_argument("--success-text", required=True)
    submit.add_argument("--max-chars", type=int, default=12000)
    submit.add_argument("--origins-json", default="[]")
    return parser


def main():
    # The direct flag conveys no authority: the worker still must be ea-bot,
    # and setting it inside Codex does not escape the network sandbox.
    if os.environ.get("EA_BROWSER_DIRECT") != "1":
        M.forward_or_none("web_browser", sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args()
    M.check_child_parse(parser, args)
    # No owner-side direct execution, even when mediation is disabled.
    if pwd.getpwuid(os.geteuid()).pw_name != M.RESIDENT_USER:
        parser.error("the web browser must run as ea-bot; it never uses the owner's identity")
    try:
        result = run(args)
    except Exception as e:
        result = {"ok": False, "submitted": False, "error": str(e)[:1000]}
    print(json.dumps(result))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
