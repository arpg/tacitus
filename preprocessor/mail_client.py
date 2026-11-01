"""
mail_client.py: one mail interface over both providers (issue #76).

Each function takes `provider` ("outlook" or "gmail") and, for Gmail, an
optional `account`, and returns one normalized shape, or an
{"error": ...} dict instead of raising. This module is a library with no
side effects; mail_actions.py is the CLI over it.

Step 1 has the reads: search, get_message, get_draft, mark_read.

Normalized message shape (search and get_message; get_message adds
"body" and "has_attachments"):
    {
        "id": str,            # message id (provider-specific, opaque)
        "thread_id": str,     # Outlook conversationId / Gmail threadId
        "subject": str,
        "from": str,          # bare address
        "from_name": str,     # display name (Outlook only)
        "to": list[str],
        "cc": list[str],
        "date": str,          # ISO8601 (Outlook) or RFC2822 (Gmail), as given
        "preview": str,       # search only
        "is_read": bool | None,
        "is_draft": bool,
        "provider": "outlook" | "gmail",
        "account": str | None,  # Gmail address; None for Outlook
    }
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

VALID_PROVIDERS = ("outlook", "gmail")


def _parse_addr(raw: str) -> str:
    """Extract the bare email address from a raw header value like
    '"Some Name" <addr@example.com>' or 'addr@example.com'."""
    m = re.search(r"<([^>]+)>", raw or "")
    return (m.group(1) if m else (raw or "")).strip()


def _parse_addr_list(raw: str) -> list[str]:
    if not raw:
        return []
    return [_parse_addr(p) for p in raw.split(",") if p.strip()]


def _default_gmail_account() -> str:
    """The first of the profile's `gmail_accounts`, or ""."""
    path = Path(os.environ.get("EA_DIR", Path.home() / ".ea")) / "config" / "user-profile.json"
    try:
        accounts = json.loads(path.read_text()).get("gmail_accounts") or []
    except (OSError, ValueError):
        return ""
    first = accounts[0] if accounts else ""
    return first if isinstance(first, str) else ""


def _resolve_gmail_account(account: str | None) -> str | None:
    return account or _default_gmail_account() or None


def _provider_error(provider: str) -> dict:
    return {"error": f"Unknown provider: {provider!r} (expected one of {VALID_PROVIDERS})"}


# -------------------------------------------------------------------
# search
# -------------------------------------------------------------------

def _normalize_outlook_search_result(m: dict) -> dict:
    from_addr = m.get("from", {}).get("emailAddress", {})
    return {
        "id": m.get("id", ""),
        "thread_id": m.get("conversationId", ""),
        "subject": m.get("subject", "(no subject)"),
        "from": from_addr.get("address", ""),
        "from_name": from_addr.get("name", ""),
        "to": [r.get("emailAddress", {}).get("address", "")
              for r in m.get("toRecipients", [])],
        "cc": [r.get("emailAddress", {}).get("address", "")
              for r in m.get("ccRecipients", [])],
        "date": m.get("receivedDateTime", ""),
        "preview": m.get("bodyPreview", ""),
        "is_read": m.get("isRead"),
        "is_draft": bool(m.get("isDraft", False)),
        "provider": "outlook",
        "account": None,
    }


def _normalize_gmail_search_result(m: dict, account: str) -> dict:
    labels = m.get("label_ids") or []
    return {
        "id": m.get("id", ""),
        "thread_id": m.get("thread_id", ""),
        "subject": m.get("subject", "(no subject)"),
        "from": _parse_addr(m.get("from", "")),
        "from_name": "",
        "to": _parse_addr_list(m.get("to", "")),
        "cc": _parse_addr_list(m.get("cc", "")),
        "date": m.get("date", ""),
        "preview": m.get("snippet", ""),
        "is_read": "UNREAD" not in labels,
        "is_draft": "DRAFT" in labels,
        "provider": "gmail",
        "account": account,
    }


def search(provider: str, query: str, account: str | None = None,
          max_results: int = 20, folder: str | None = None) -> list[dict]:
    """Search mail on either provider, normalized to one schema.

    provider: "outlook" | "gmail"
    query: Outlook uses Graph's $search syntax; Gmail uses Gmail search
        syntax (e.g. "is:unread in:inbox"). These are NOT the same
        query language and this function does not translate between them.
    account: Gmail address. Required only in the sense that Gmail has
        multiple possible accounts; if omitted, resolves to the
        configured default personal account. Ignored for Outlook
        (single work mailbox, authenticated via MSAL, no account
        concept).
    """
    provider = (provider or "").lower()
    if provider == "outlook":
        import graph_client as graph
        try:
            results, _truncated = graph.search_messages(query, top=max_results, folder=folder)
        except LookupError as e:  # an unknown --folder
            return [{"error": str(e)}]
        except Exception as e:
            return [{"error": f"Could not search: {e}"}]
        return [_normalize_outlook_search_result(m) for m in results]
    elif provider == "gmail":
        if folder:
            return [{"error": "Gmail has labels, not folders: put label:<name> in --query"}]
        import gmail_client
        acct = _resolve_gmail_account(account)
        if not acct:
            return [{"error": "No Gmail account configured"}]
        results = gmail_client.search_messages(acct, query, max_results=max_results)
        errors = [r for r in results if "error" in r]
        if errors:
            return errors
        return [_normalize_gmail_search_result(m, acct) for m in results]
    return [_provider_error(provider)]


# -------------------------------------------------------------------
# get_message
# -------------------------------------------------------------------

def _strip_html(html: str, limit: int = 5000) -> str:
    """Same HTML-stripping approach as graph_actions.py's cmd_get_message
    (preserve links, collapse tags to newlines, unescape entities),
    duplicated here rather than imported since graph_actions.py pulls in
    graph_client at module load time (live MSAL network call), which
    this module must not depend on for the Gmail path."""
    import html as html_mod
    stripped = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    stripped = re.sub(r'<script[^>]*>.*?</script>', '', stripped, flags=re.DOTALL)
    stripped = re.sub(
        r'<a[^>]*?href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
        r'\2 [\1]', stripped, flags=re.DOTALL,
    )
    stripped = re.sub(r'<[^>]+>', '\n', stripped)
    stripped = html_mod.unescape(stripped)
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    return '\n'.join(lines)[:limit]


def _normalize_outlook_get_message(m: dict) -> dict:
    from_addr = m.get("from", {}).get("emailAddress", {})
    body_content = m.get("body", {}).get("content", "")
    body_type = m.get("body", {}).get("contentType", "text")
    body = _strip_html(body_content) if body_type.lower() == "html" else body_content[:5000]
    return {
        "id": m.get("id", ""),
        "thread_id": m.get("conversationId", ""),
        "subject": m.get("subject", "(no subject)"),
        "from": from_addr.get("address", ""),
        "from_name": from_addr.get("name", ""),
        "to": [r.get("emailAddress", {}).get("address", "")
              for r in m.get("toRecipients", [])],
        "cc": [r.get("emailAddress", {}).get("address", "")
              for r in m.get("ccRecipients", [])],
        "date": m.get("receivedDateTime", ""),
        "body": body,
        "has_attachments": m.get("hasAttachments", False),
        "is_draft": bool(m.get("isDraft", False)),
        "provider": "outlook",
        "account": None,
    }


def _normalize_gmail_get_message(m: dict, account: str) -> dict:
    body = m.get("body", "")
    if "<html" in body.lower() or "<body" in body.lower():
        body = _strip_html(body)
    else:
        body = body[:5000]
    return {
        "id": m.get("id", ""),
        "thread_id": m.get("thread_id", ""),
        "subject": m.get("subject", "(no subject)"),
        "from": _parse_addr(m.get("from", "")),
        "from_name": "",
        "to": _parse_addr_list(m.get("to", "")),
        "cc": _parse_addr_list(m.get("cc", "")),
        "date": m.get("date", ""),
        "body": body,
        "has_attachments": m.get("has_attachments", False),
        "is_draft": "DRAFT" in (m.get("label_ids") or []),
        "provider": "gmail",
        "account": account,
    }


def get_message(provider: str, message_id: str, account: str | None = None) -> dict:
    """Fetch a full message (with body) on either provider, normalized.

    NOTE on `date`: Outlook returns ISO8601 UTC; Gmail returns an
    RFC2822 Date header. Not normalized to one format:
    callers comparing dates across providers should parse accordingly
    (see preprocess.py's detect_gmail_needs_response for an RFC2822
    parse example). Revisit if this becomes a real pain point.
    """
    provider = (provider or "").lower()
    if provider == "outlook":
        import graph_client as graph
        try:
            m = graph.get_message_full(message_id)
        except Exception as e:
            return {"error": f"Could not fetch message: {e}"}
        return _normalize_outlook_get_message(m)
    elif provider == "gmail":
        import gmail_client
        acct = _resolve_gmail_account(account)
        if not acct:
            return {"error": "No Gmail account configured"}
        m = gmail_client.get_message(acct, message_id)
        if "error" in m:
            return m
        return _normalize_gmail_get_message(m, acct)
    return _provider_error(provider)


def get_draft(provider: str, draft_id: str, account: str | None = None) -> dict:
    """A draft's {to, cc, subject, body, body_type, conversation_id,
    message_id}, with the RAW body: a confirmation preview or a body
    rewrite needs it whole, so this does not go through get_message's
    HTML strip and 5000-character cut.

    `body_type` is "Text" or "HTML". Gmail's is always "Text":
    gmail_client.get_draft already flattens an HTML part to text.
    """
    provider = (provider or "").lower()
    if provider == "outlook":
        import graph_client as graph
        try:
            m = graph.get_message_full(draft_id)
        except Exception as e:
            return {"error": f"Could not fetch draft: {e}"}
        # Exactly "Text" or "HTML": this value flows straight into
        # --content-type args elsewhere (choices=["Text", "HTML"]);
        # .capitalize() would silently produce the invalid "Html".
        raw_content_type = (m.get("body", {}).get("contentType") or "text").lower()
        body_type = "HTML" if raw_content_type == "html" else "Text"
        return {
            "to": ", ".join(r.get("emailAddress", {}).get("address", "")
                            for r in m.get("toRecipients", [])),
            "cc": ", ".join(r.get("emailAddress", {}).get("address", "")
                            for r in m.get("ccRecipients", [])),
            "subject": m.get("subject", "(no subject)"),
            "body": m.get("body", {}).get("content", ""),
            "body_type": body_type,
            "conversation_id": m.get("conversationId", ""),
            "message_id": m.get("id", draft_id),
            "provider": "outlook", "account": None,
        }
    elif provider == "gmail":
        import gmail_client
        acct = _resolve_gmail_account(account)
        if not acct:
            return {"error": "No Gmail account configured"}
        result = gmail_client.get_draft(acct, draft_id)
        if "error" in result:
            return result
        result.setdefault("body_type", "Text")
        result["provider"] = "gmail"
        result["account"] = acct
        return result
    return _provider_error(provider)


# -------------------------------------------------------------------
# mark_read
# -------------------------------------------------------------------

def mark_read(provider: str, message_id: str, account: str | None = None) -> dict:
    """Mark a message read in place on either provider; it is not moved or
    archived. Ungated: only the user's own read flag changes."""
    provider = (provider or "").lower()
    if provider == "outlook":
        import graph_client as graph
        try:
            graph.mark_message_read(message_id)
        except Exception as e:
            return {"error": f"Could not mark the message read: {e}"}
        return {"status": "marked_read", "id": message_id, "provider": "outlook", "account": None}
    elif provider == "gmail":
        import gmail_client
        acct = _resolve_gmail_account(account)
        if not acct:
            return {"error": "No Gmail account configured"}
        result = gmail_client.mark_read(acct, message_id)
        if "error" in result:
            return {"error": result["error"]}
        return {"status": "marked_read", "id": message_id, "provider": "gmail", "account": acct}
    return _provider_error(provider)
