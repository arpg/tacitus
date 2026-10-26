#!/usr/bin/env python3
"""
email_to_pdf.py — Convert an Outlook or Gmail email body to PDF for receipt submission.

Uses chromium headless (via playwright) to "print" the HTML email body to PDF,
matching what you'd get from File > Print > Save as PDF in a browser. The
renderer runs offline with scripts off and aborts every request the mail's
HTML makes (remote images included), so a PDF never fetches anything; the
count of refused requests goes on the reviewed outbound log.

Usage:
    python3 email_to_pdf.py --message-id <id> --output <path.pdf>
    python3 email_to_pdf.py --gmail <account> --message-id <id> --output <path.pdf>

Or as a library:
    from email_to_pdf import message_to_pdf
    path = message_to_pdf(message_id, output_path)
    path = message_to_pdf(message_id, output_path, gmail="you@gmail.com")
"""

import argparse
import html as _html
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outbound_log  # noqa: E402


def _fetch_message(message_id: str, graph_module=None) -> dict:
    """Return {subject, sender, date, body, body_type} for an Outlook message.

    Goes through the mediated `graph_actions.py get-message` CLI rather than
    importing graph_client. Under mail mediation the Graph token cache is
    owner-owned, so an ea-bot-side `import graph_client` raises PermissionError
    at module load and takes the whole PDF path with it. `get-message` is a
    SAFE verb, so the subprocess is forwarded to the owner-side mediator and works
    for both users. `--raw-html` keeps the untruncated HTML body, which is
    what gets printed.

    Callers that already hold a graph module (owner-side tooling with a token)
    can pass it in and skip the subprocess.
    """
    if graph_module is not None:
        import mail_mediation as _mm
        if not _mm.is_plain_id(message_id):
            raise ValueError(f"message id {str(message_id)[:24]!r} is not a plain mail id")
        msg = graph_module.graph_get(
            f"/me/messages/{message_id}",
            {"$select": "subject,from,receivedDateTime,body"})
        return {
            "subject": msg.get("subject", "Email"),
            "sender": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
            "date": msg.get("receivedDateTime", ""),
            "body": msg.get("body", {}).get("content", ""),
            "body_type": msg.get("body", {}).get("contentType", "text").lower(),
        }

    msg = _run_get_message("graph_actions.py",
                           ["get-message", "--message-id", message_id, "--raw-html"])
    return {
        "subject": msg.get("subject") or "Email",
        "sender": (msg.get("from") or {}).get("address", ""),
        "date": msg.get("received", ""),
        "body": msg.get("body", ""),
        "body_type": (msg.get("body_type") or "text").lower(),
    }


def _fetch_gmail_message(account: str, message_id: str) -> dict:
    """The same fields for a message in a Gmail account, through the
    mediated `gmail_client.py get-message` (a SAFE verb). Its `html` is the
    raw text/html part; a message with none prints its plain text."""
    msg = _run_get_message("gmail_client.py",
                           ["get-message", "--email", account, "--message-id", message_id])
    rich = msg.get("html") or ""
    return {
        "subject": msg.get("subject") or "Email",
        "sender": msg.get("from", ""),
        "date": msg.get("date", ""),
        "body": rich or msg.get("body", ""),
        "body_type": "html" if rich else "text",
    }


def _run_get_message(script: str, argv: list) -> dict:
    """Run a mail client's get-message and return its JSON object."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / script), *argv],
        capture_output=True, text=True,
    )
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            f"{Path(script).stem} get-message failed (rc={proc.returncode}): "
            f"{(proc.stderr or out or '').strip()[:400]}")
    try:
        msg = json.loads(out)
    except json.JSONDecodeError:
        # Be tolerant of any banner printed ahead of the JSON object.
        brace = out.find("{")
        if brace < 0:
            raise RuntimeError(f"unparseable get-message output: {out[:400]}")
        msg = json.loads(out[brace:])
    if msg.get("error"):
        raise RuntimeError(msg["error"])
    return msg


def message_to_pdf(message_id: str, output_path: str, graph_module=None,
                   gmail: str = "") -> str:
    """
    Fetch an Outlook message, or with `gmail` a message in that Gmail
    account, and save its body as a PDF.

    Returns the output path on success, raises on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium")

    if gmail:
        msg = _fetch_gmail_message(gmail, message_id)
    else:
        msg = _fetch_message(message_id, graph_module)
    html = _page_html(msg)

    output_path = str(output_path)
    refused = Refused()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # The mail's HTML is untrusted: no script runs, the context is
            # offline, and every request the page makes (an image, a font, a
            # tracking pixel) is aborted, so rendering a receipt reaches no
            # server and confirms nothing to its sender.
            context = browser.new_context(java_script_enabled=False, offline=True)
            page = context.new_page()
            page.route("**/*", refused.route)
            page.set_content(html, wait_until="load")
            page.pdf(path=output_path, format="Letter", print_background=True,
                     margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"})
        finally:
            browser.close()
    refused.log()

    return output_path


def _page_html(msg: dict) -> str:
    """The page that is printed: the message's From, Subject and Date
    above its body, since the PDF is a receipt record."""
    body_content = msg["body"]
    # A Gmail From header is "Name <address>": escaped, or the address
    # parses as a tag and drops out of the header.
    subject = _html.escape(msg["subject"])
    sender = _html.escape(msg["sender"])
    date = _html.escape(msg["date"])

    if msg["body_type"] == "text":
        body_content = f"<pre style='white-space: pre-wrap'>{_html.escape(body_content)}</pre>"

    header_html = f"""
    <div style="font-family: Arial, sans-serif; border-bottom: 1px solid #ccc; padding-bottom: 8px; margin-bottom: 16px;">
      <strong>From:</strong> {sender}<br>
      <strong>Subject:</strong> {subject}<br>
      <strong>Date:</strong> {date}
    </div>
    """
    if re.search(r"<body", body_content, re.IGNORECASE):
        return re.sub(r"<body", lambda _m: f"<body><div id='ea-header'>{header_html}</div><div id='ea-body'",
                      body_content, count=1, flags=re.IGNORECASE) + "</div>"
    return f"<html><body>{header_html}{body_content}</body></html>"


class Refused:
    """The page route that aborts every request but inline data, with a
    tally of what it refused for the reviewed outbound log."""
    LOCAL = ("data:", "about:", "blob:")

    def __init__(self):
        self.count = 0
        self.hosts: dict[str, int] = {}

    def route(self, route, request=None) -> None:
        url = str(getattr(request or route.request, "url", "") or "")
        try:
            if url.startswith(self.LOCAL):
                route.continue_()
                return
            self.count += 1
            host = outbound_log.host_of(url) or url.split(":", 1)[0]
            self.hosts[host] = self.hosts.get(host, 0) + 1
            route.abort()
        except Exception:
            pass

    def log(self) -> None:
        if self.count:
            top = sorted(self.hosts.items(), key=lambda kv: -kv[1])[:10]
            outbound_log.note("render", "email_to_pdf", refused=self.count,
                              hosts=[h for h, _ in top])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an Outlook or Gmail email to PDF",
                                     allow_abbrev=False)
    parser.add_argument("--message-id", required=True,
                        help="Outlook message ID, or with --gmail a Gmail message ID")
    parser.add_argument("--gmail", default="",
                        help="Gmail account the message is in (gmail_client.py --email); "
                             "without it the message is Outlook mail")
    parser.add_argument("--output", required=True, help="Output PDF path")
    args = parser.parse_args()

    try:
        path = message_to_pdf(args.message_id, args.output, gmail=args.gmail)
        print(f"Saved: {path}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
