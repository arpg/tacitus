#!/usr/bin/env python3
"""
mail_actions.py: one mail CLI for both providers (issue #76).

Every verb takes --provider outlook|gmail and, for Gmail, an optional
--account (the first of the profile's gmail_accounts when omitted).

Step 1 has the reads:
    python3 mail_actions.py search --provider outlook --query "<q>" [--folder <name>]
    python3 mail_actions.py search --provider gmail --query "is:unread" [--account <addr>]
    python3 mail_actions.py get-message --provider outlook|gmail --message-id <id>
    python3 mail_actions.py get-draft --provider outlook|gmail --draft-id <id>
    python3 mail_actions.py mark-read --provider outlook|gmail --message-id <id>
    python3 mail_actions.py mark-unread --provider outlook|gmail --message-id <id>

Step 2 has the draft writes:
    python3 mail_actions.py create-draft --provider outlook|gmail --to <a,b> --subject <s> --body-file <f> [--cc <a>] [--bcc <a>] [--ledger-id <id>]...
    python3 mail_actions.py create-reply --provider outlook|gmail --message-id <id> --body-file <f> [--sender-only] [--force] [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py create-reply --provider gmail --thread-id <id> --body-file <f> [--no-quote]
    python3 mail_actions.py update-draft --provider outlook|gmail --draft-id <id> --body-file <f> [--to <a>] [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py update-draft --provider gmail --draft-id <id> --body-file <f> [--reply-to-message-id <id>] [--no-quote]
    python3 mail_actions.py delete-draft --provider outlook|gmail --draft-id <id>

Step 5, the bot's only (GATED: refused for the EA, denied in every lane):
    python3 mail_actions.py send-draft --provider outlook|gmail --draft-id <id> --confirm-token <t>

Step 3:
    python3 mail_actions.py create-forward --provider outlook|gmail --message-id <id> --to <a,b> --body-file <f> [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py preflight-check --provider outlook|gmail --draft-id <id>

Images (an attachment or a picture shown in the body, the inline cid: kind):
    python3 mail_actions.py describe-attachment --provider outlook|gmail --message-id <id> (--attachment-id <id> | --name <substring> | --all-images) [--question "<what to read off it>"] [--output <file>]
The image is fetched and read in the owner's process by the local model
that reads images (remote_llm.image_model), and the lane gets its text.
With no such model the answer is {"need": "no image model"}, and outside
sealed mail the one image picked is saved to --output for the lane to
open (Read on Claude, view_image on Codex).

Every verb runs the provider CLI's own verb in this process (DELEGATE):
graph_actions on Outlook, gmail_client on Gmail. Its journaling
(draft_events), its checks and search-mail's notes are the provider
verb's own; an Outlook verb runs through graph_actions.run_verb, so a
throttled mailbox ends the same way. _shape adds the unified fields to
what that verb prints and drops nothing:
    id, provider, account                every verb
    thread_id, conversation_id, from,    search rows and get-message
    from_name, to, cc (addresses), date
A reply is reply-all unless --sender-only, on both providers, and
create-reply skips a thread whose latest message is the user's own on
both (--force overrides).

The entry point does what graph_actions.py's does: it resolves a run's
short id handles, and under mail mediation it forwards the call to the
owner-side mediator, which runs it with the token.
"""
import argparse
import email.utils as eu
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# verb -> provider -> (tool, verb): the provider verb a unified verb runs.
DELEGATE = {
    "search": {"outlook": ("graph_actions", "search-mail"), "gmail": ("gmail_client", "search")},
    "get-message": {"outlook": ("graph_actions", "get-message"),
                    "gmail": ("gmail_client", "get-message")},
    # graph_actions reads an Outlook draft with get-message --raw-html.
    "get-draft": {"outlook": ("graph_actions", "get-message"),
                  "gmail": ("gmail_client", "get-draft")},
    "mark-read": {"outlook": ("graph_actions", "mark-read"), "gmail": ("gmail_client", "mark-read")},
    "mark-unread": {"outlook": ("graph_actions", "mark-unread"),
                    "gmail": ("gmail_client", "mark-unread")},
    "create-draft": {"outlook": ("graph_actions", "create-draft"),
                     "gmail": ("gmail_client", "create-draft")},
    "create-reply": {"outlook": ("graph_actions", "create-reply"),
                     "gmail": ("gmail_client", "create-draft")},
    "update-draft": {"outlook": ("graph_actions", "update-draft"),
                     "gmail": ("gmail_client", "update-draft")},
    "delete-draft": {"outlook": ("graph_actions", "delete-draft"),
                     "gmail": ("gmail_client", "delete-draft")},
    "create-forward": {"outlook": ("graph_actions", "create-forward"),
                       "gmail": ("gmail_client", "create-forward")},
    "preflight-check": {"outlook": ("graph_actions", "preflight-check"),
                        "gmail": ("gmail_client", "preflight-check")},
    # Each provider verb validates and consumes the one-time token, then sends
    # only while the draft still has the fingerprint the token names.
    "send-draft": {"outlook": ("graph_actions", "send-draft"),
                   "gmail": ("gmail_client", "send-draft")},
}


def _gmail_account(account: str | None) -> str:
    """`account`, else the first of the profile's gmail_accounts, else ""."""
    if account:
        return account
    path = Path(os.environ.get("EA_DIR", Path.home() / ".ea")) / "config" / "user-profile.json"
    try:
        accounts = json.loads(path.read_text()).get("gmail_accounts") or []
    except (OSError, ValueError):
        return ""
    first = accounts[0] if accounts else ""
    return first if isinstance(first, str) else ""


def _fail(msg: str, code: int = 2) -> int:
    print(json.dumps({"error": msg}, indent=2))
    return code


def _addrs(*headers) -> list[str]:
    import email.utils as eu
    # getaddresses parses nothing at all when one header is empty (3.12)
    return [a for _n, a in eu.getaddresses([h for h in headers if h]) if a]


def _gmail_answered(acct: str, message_id: str, thread_id: str) -> tuple[dict, str]:
    """(the message a Gmail reply answers, error). With a thread id it is
    the message gmail_client answers: the thread's newest one that is not
    a draft."""
    import gmail_client
    if not message_id:
        service = gmail_client._get_service(acct)
        message_id = service and gmail_client._newest_message_in_thread(service, thread_id)
        if not message_id:
            return {}, f"no message found in Gmail thread {thread_id}"
    orig = gmail_client.get_message(acct, message_id)
    if "error" in orig:
        return {}, orig["error"]
    return orig, ""


def _gmail_user_replied(acct: str, orig: dict) -> str:
    """The sender of `orig` when it is the account itself: the user sent the
    thread's latest message, so there is nothing to answer (the skip
    graph_actions create-reply makes on Outlook)."""
    sender = _addrs(orig.get("from"))
    return sender[0] if sender and sender[0].lower() == acct.lower() else ""


def _gmail_reply_cc(acct: str, orig: dict, extra: str) -> str:
    """The Cc of a Gmail reply-all: the answered message's To and Cc and the
    caller's --cc, less the account itself and the sender (who gets the To
    line from gmail_client)."""
    addrs = _addrs
    skip = {acct.lower()} | {a.lower() for a in addrs(orig.get("from"), orig.get("reply_to"))}
    out = []
    for a in addrs(orig.get("to"), orig.get("cc"), extra):
        if a.lower() not in skip and a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return ",".join(out)


def _provider_argv(args, acct: str | None) -> tuple[list[str], str]:
    """(the provider verb's argv, error)."""
    verb, outlook = args.command, args.provider == "outlook"
    argv = [DELEGATE[verb][args.provider][1]]
    if not outlook:
        argv += ["--email", acct]

    def opt(flag, value):
        if value:
            argv.extend([flag, value])

    if verb == "search":
        if not outlook and args.folder:
            return [], "Gmail has labels, not folders: put label:<name> in --query"
        argv += ["--query", args.query,
                 "--top" if outlook else "--max-results", str(args.max_results)]
        opt("--folder", args.folder)
        return argv, ""
    if verb in ("get-message", "mark-read", "mark-unread"):
        if args.command == "get-message" and args.raw_html and not outlook:
            return [], "--raw-html is Outlook only (a Gmail body is text)"
        argv += ["--message-id", args.message_id]
        if getattr(args, "raw_html", False):
            argv.append("--raw-html")
        return argv, ""
    if verb == "get-draft":
        # the raw body: a preview or a rewrite needs the draft whole
        if outlook and args.with_fingerprint:
            return [], "--with-fingerprint is Gmail only"
        argv += ["--message-id", args.draft_id, "--raw-html"] if outlook \
            else ["--draft-id", args.draft_id]
        if args.with_fingerprint:
            argv.append("--with-fingerprint")
        return argv, ""
    if verb == "send-draft":
        if outlook and args.fingerprint:
            return [], "--fingerprint is Gmail only (Outlook takes it from the token)"
        # one argv element each: a value that starts with "-" is not an option
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id,
                 f"--confirm-token={args.confirm_token or ''}"]
        if args.fingerprint:
            argv.append(f"--fingerprint={args.fingerprint}")
        return argv, ""
    if verb not in ("delete-draft", "preflight-check"):
        # graph_actions reads --body-file over --body, which it requires.
        argv += (["--body", "-", "--body-file", args.body_file] if outlook
                 else ["--body-file", args.body_file])
        for lid in args.ledger_id or []:
            argv += ["--ledger-id", lid]
    if verb == "create-draft":
        if outlook and args.bcc:
            return [], "Outlook drafts take no --bcc"
        opt("--to", args.to)
        opt("--subject", args.subject)
        opt("--cc", args.cc)
        if not outlook:
            opt("--bcc", args.bcc)
        elif args.force_new_thread:
            argv.append("--force-new-thread")
    elif verb == "create-reply":
        if outlook:
            if args.thread_id or args.no_quote or args.subject or args.bcc:
                return [], "--thread-id, --no-quote, --subject and --bcc are Gmail only"
            if not args.message_id:
                return [], "--message-id is required"
            argv += ["--message-id", args.message_id]
            opt("--conversation-id", args.conversation_id)
            opt("--to", args.to)
            opt("--cc", args.cc)
            if args.sender_only:
                argv.append("--sender-only")
            if args.force:
                argv.append("--force")
        else:
            if args.message_id:
                argv += ["--reply-to-message-id", args.message_id]
            else:
                argv += ["--thread-id", args.thread_id]
            cc = args.cc
            # --to replaces the thread's people, as on Outlook: no reply-all Cc
            if not args.sender_only and not args.to:
                cc = _gmail_reply_cc(acct, args._answered, args.cc)
            opt("--to", args.to)
            opt("--subject", args.subject)
            opt("--cc", cc)
            opt("--bcc", args.bcc)
            if args.no_quote:
                argv.append("--no-quote")
    elif verb == "update-draft":
        if outlook and args.subject:
            return [], "an Outlook draft's subject does not change in update-draft"
        if outlook and (args.reply_to_message_id or args.no_quote or args.bcc or args.thread_id):
            return [], "--reply-to-message-id, --thread-id, --no-quote and --bcc are Gmail only"
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id]
        opt("--to", args.to)
        opt("--cc", args.cc)
        if not outlook:
            opt("--subject", args.subject)
            opt("--bcc", args.bcc)
            opt("--reply-to-message-id", args.reply_to_message_id)
            opt("--thread-id", args.thread_id)
            if args.no_quote:
                argv.append("--no-quote")
    elif verb == "create-forward":
        if outlook and args.bcc:
            return [], "Outlook drafts take no --bcc"
        argv += ["--message-id", args.message_id, "--to", args.to]
        opt("--cc", args.cc)
        if not outlook:
            opt("--bcc", args.bcc)
    else:  # delete-draft, preflight-check
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id]
    if outlook and getattr(args, "content_type", None):
        argv += ["--content-type", args.content_type]
    return argv, ""


def _addr(v) -> str:
    """The bare address of a Graph {name, address}, or of a header value."""
    if isinstance(v, dict):
        return v.get("address", "")
    return eu.parseaddr(v or "")[1] or (v or "")


def _addr_list(v) -> list[str]:
    if isinstance(v, list):
        return [a for a in (_addr(x) for x in v) if a]
    return [a for _n, a in eu.getaddresses([v]) if a] if v else []


def _shape_message(m: dict, provider: str) -> dict:
    """A search row or a message with the unified fields added."""
    if not isinstance(m, dict) or "id" not in m:
        return m   # a note row, an error
    frm = m.get("from")
    m["from_name"] = frm.get("name", "") if isinstance(frm, dict) else eu.parseaddr(frm or "")[0]
    m["from"] = _addr(frm)
    for k in ("to", "cc"):
        if k in m:
            m[k] = _addr_list(m[k])
    conv = m.get("conversation_id") or m.get("thread_id") or ""
    m["thread_id"] = m["conversation_id"] = conv
    m.setdefault("date", m.get("received", ""))
    if provider == "gmail":
        labels = m.get("label_ids") or []
        m.setdefault("preview", m.get("snippet", ""))
        if "label_ids" in m:
            m["is_read"], m["is_draft"] = "UNREAD" not in labels, "DRAFT" in labels
    return m


def _shape(verb: str, provider: str, result):
    if isinstance(result, list):
        return [_shape_message(m, provider) for m in result]
    if not isinstance(result, dict) or "error" in result:
        return result
    if verb == "get-message":
        return _shape_message(result, provider)
    if verb == "get-draft" and provider == "outlook":
        # get-message --raw-html: the raw body stays "body"
        kind = (result.get("body_type") or "text").lower()
        return {"to": ", ".join(_addr_list(result.get("to"))),
                "cc": ", ".join(_addr_list(result.get("cc"))),
                "subject": result.get("subject") or "(no subject)",
                "body": result.get("body", ""),
                "body_type": "HTML" if kind == "html" else "Text",
                "conversation_id": result.get("conversation_id") or "",
                "message_id": result.get("id", ""), "id": result.get("id", "")}
    if verb == "get-draft":
        result.setdefault("body_type", "Text")
    if verb in ("mark-read", "mark-unread"):
        result["status"] = "marked_read" if verb == "mark-read" else "marked_unread"
    return result


def cmd_delegated(args) -> int:
    """Every verb in DELEGATE: run the provider verb, then add the unified
    fields to what it prints."""
    acct = None
    if args.provider == "gmail":
        acct = _gmail_account(args.account)
        if not acct:
            return _fail("No Gmail account configured")
    if getattr(args, "body_file", None) is not None:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as e:
            return _fail(f"cannot read --body-file: {e}")
        if not body.strip():
            return _fail("the body is empty: write the text to --body-file")
    if args.command == "create-reply" and args.provider == "gmail" and not args.to:
        # gmail_client has no already-replied skip: make graph_actions's here.
        # --to names the people, so the thread is not read for them.
        args._answered, err = _gmail_answered(acct, args.message_id, args.thread_id)
        if err:
            return _fail(err)
        sender = _gmail_user_replied(acct, args._answered)
        if sender and not args.force:
            print(json.dumps({
                "status": "skipped",
                "reason": ("user already sent the latest message in this "
                           "thread; not drafting a reply"),
                "latest_sender": sender,
                "override": ("when the user asked for this reply in the "
                             "current thread, run the same create-reply "
                             "with --force"),
                "provider": "gmail", "account": acct,
            }, indent=2))
            return 0
    pargv, err = _provider_argv(args, acct)
    if err:
        return _fail(err)
    tool = DELEGATE[args.command][args.provider][0]
    out, rc, saved = io.StringIO(), 0, sys.argv
    try:
        with redirect_stdout(out):
            if tool == "graph_actions":
                import graph_actions
                pargs = graph_actions.build_parser().parse_args(pargv)
                graph_actions.run_verb(pargs)
            else:
                import gmail_client
                sys.argv = ["gmail_client.py", *pargv]
                if args._mediated:
                    # The mediator holds this draft's lock: gmail_client must
                    # not take it again (it would wait on its own mediator).
                    import mail_mediation
                    os.environ[mail_mediation.MEDIATED_VERB_ENV] = pargv[0]
                gmail_client.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = saved
    text = out.getvalue()
    try:
        result = json.loads(text)
    except ValueError:
        sys.stdout.write(text)
        return rc
    result = _shape(args.command, args.provider, result)
    for row in (result if isinstance(result, list) else [result]):
        if isinstance(row, dict) and ("id" in row or row is result):
            row.setdefault("id", row.get("draft_id") or row.get("message_id") or "")
            row["provider"], row["account"] = args.provider, acct
    if (args.command == "get-message" and args.provider == "gmail" and isinstance(result, dict)
            and "error" not in result):
        # graph_actions adds the Outlook line itself.
        import mail_images
        if mail_images.content_images([a for a in result.get("attachments") or []
                                       if isinstance(a, dict)]):
            result["images_hint"] = mail_images.hint("gmail", result.get("id") or "", acct or "")
    if isinstance(result, dict) and "error" in result and rc == 0:
        rc = 1
    print(json.dumps(result, indent=2))
    return rc


# ── describe-attachment: an image read as text ───────────────────────
# The verb runs where the mail token is (the mediator's child for the
# resident): the image is fetched and read there, and the answer is text.
# The model calls share one budget that ends before the mediator's own
# limit (mail_mediator.EXEC_TIMEOUT_S, 120 s), so a cold model ends in an
# answer, not a killed child.
DESCRIBE_BUDGET_S = 90.0
_ROW_KEYS = ("attachment_id", "name", "content_type", "size", "is_inline", "is_image", "small")


def _attachment_source(provider: str, acct: str | None, message_id: str):
    """(rows, fetch, error): every attachment of the message, inline images
    included, in one shape (attachment_id, name, content_type, size,
    is_inline, content_id, is_image, small), and fetch(row) -> bytes, or
    {"error"} for a row that cannot be read."""
    import mail_images
    if provider == "gmail":
        import gmail_client
        return gmail_client.attachment_source(acct, message_id)
    import graph_client
    try:
        atts = graph_client.get_attachment_list(message_id)
    except Exception as e:
        return [], None, f"Could not list the attachments: {str(e)[:200]}"
    rows = [mail_images.shape({"attachment_id": a.get("id") or "", "name": a.get("name") or "",
                               "content_type": a.get("contentType") or "",
                               "size": a.get("size") or 0, "is_inline": bool(a.get("isInline")),
                               "content_id": ""})
            for a in atts if isinstance(a, dict)]

    def fetch(row):
        try:
            return graph_client.download_attachment(message_id, row["attachment_id"])
        except Exception as e:
            return {"error": f"Could not fetch {row.get('name') or 'the attachment'}: {str(e)[:200]}"}
    return rows, fetch, ""


def _pick_images(rows: list[dict], args) -> tuple[list[dict], dict]:
    """(the image rows the call names, an error answer or {})."""
    import mail_images
    listing = [{k: r[k] for k in _ROW_KEYS if k in r} for r in rows]
    if args.all_images:
        picked = mail_images.content_images(rows)
        if not picked:
            # Only small inline images (logos, spacers): read those rather
            # than nothing.
            picked = [r for r in rows if r.get("is_image")]
        return picked[:mail_images.MAX_IMAGES], {}
    if args.attachment_id:
        hits = [r for r in rows if r.get("attachment_id") == args.attachment_id]
    else:
        pat = (args.name or "").lower()
        hits = [r for r in rows if pat and pat in (r.get("name") or "").lower()]
    if not hits:
        return [], {"error": "No attachment matched", "attachments": listing}
    if len(hits) > 1:
        return [], {"error": "Several attachments matched; pass --attachment-id",
                    "matches": [{k: r[k] for k in _ROW_KEYS if k in r} for r in hits]}
    if not hits[0].get("is_image"):
        return [], {"error": ("not an image: read a PDF with scan-attachments, or "
                              "download-attachment and then doc_text.py"),
                    "attachment": {k: hits[0][k] for k in _ROW_KEYS if k in hits[0]}}
    return hits, {}


def cmd_describe_attachment(args) -> int:
    """An image on a message (a file, or one the body shows inline) read
    as text by the local image model. With none, {"need": "no image
    model"}; outside sealed mail the one image picked is saved to --output
    for the lane to open. Exit 0 with an answer, 1 with an error."""
    import time
    import mail_images
    acct = None
    if args.provider == "gmail":
        acct = _gmail_account(args.account)
        if not acct:
            return _fail("No Gmail account configured")
    rows, fetch, err = _attachment_source(args.provider, acct, args.message_id)
    if err:
        return _fail(err, 1)
    picked, problem = _pick_images(rows, args)
    if problem:
        print(json.dumps(problem, indent=2))
        return 1
    out = {"message_id": args.message_id, "provider": args.provider, "account": acct,
           "images": [{k: r[k] for k in _ROW_KEYS if k in r} for r in picked]}
    if not picked:
        out["note"] = "this message carries no image, inline ones included"
        print(json.dumps(out, indent=2))
        return 0
    model = mail_images.image_model()
    sealed = mail_images.sealed()
    datas: dict[int, bytes] = {}
    if model:
        deadline = time.monotonic() + DESCRIBE_BUDGET_S
        read = 0
        for i, (row, item) in enumerate(zip(picked, out["images"])):
            left = deadline - time.monotonic()
            if left < 5:
                item["error"] = "not read: the time for this call ran out"
                continue
            data = fetch(row)
            if isinstance(data, dict):
                item["error"] = data.get("error") or "not fetched"
                continue
            datas[i] = data
            ctype = mail_images.image_type(row.get("name") or "", row.get("content_type") or "")
            try:
                res = mail_images.describe(data, ctype, model=model, question=args.question or "",
                                           timeout=min(75.0, left))
            except Exception as e:  # RemoteLLMError, an image the model cannot take
                item["error"] = f"the image model did not read it: {str(e)[:200]}"
                continue
            item["text"] = res["text"]
            read += 1
        if read:
            out["read_by"] = "local image model"
            out["_note"] = mail_images.DATA_NOTE
        else:
            out["need"] = "the image model did not answer"
    else:
        out["need"] = "no image model"
    if "need" in out:
        if sealed:
            out["sealed"] = True
            out["message"] = ("Mail is sealed: the image stays with the mail. Say that the "
                              "image could not be read here, and what it is (name, size).")
        elif not args.output:
            out["message"] = ("Run again with --attachment-id <id> --output <a file in your "
                              "EA state folder>, then open that file (the Read tool on "
                              "Claude, view_image on Codex).")
    # The lane may open the picture itself only outside sealed mail.
    if args.output and not sealed:
        if len(picked) != 1:
            out["output_not_written"] = "--output takes one image: pass --attachment-id"
        else:
            data = datas.get(0)
            if data is None:
                data = fetch(picked[0])
            if isinstance(data, dict):
                out["output_not_written"] = data.get("error") or "not fetched"
            else:
                dest = Path(args.output).expanduser()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                # As given: the mediator reports its staging path back in
                # the caller's own path name.
                out["saved"] = args.output
    print(json.dumps(out, indent=2))
    return 0


PROVIDERS = ("outlook", "gmail")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    acct_help = ("Gmail address (ignored for outlook; defaults to the profile's "
                 "first gmail_accounts entry)")

    p_search = sub.add_parser("search", allow_abbrev=False, help="Search mail")
    p_search.add_argument("--provider", required=True, choices=PROVIDERS)
    p_search.add_argument("--account", default=None, help=acct_help)
    p_search.add_argument("--query", required=True,
                          help="Outlook: Graph $search syntax. Gmail: Gmail search syntax. "
                               "These are not the same query language.")
    p_search.add_argument("--max-results", "--top", type=int, default=20, dest="max_results")
    p_search.add_argument("--folder", default=None,
                          help="Outlook only: search inside one folder (inbox, sentitems, "
                               "archive, or a folder's own name). Gmail: label:<name> in --query.")
    p_search.set_defaults(func=cmd_delegated)

    p_get = sub.add_parser("get-message", allow_abbrev=False, help="Fetch a message with its body")
    p_get.add_argument("--provider", required=True, choices=PROVIDERS)
    p_get.add_argument("--account", default=None, help=acct_help)
    p_get.add_argument("--message-id", required=True, dest="message_id")
    p_get.add_argument("--raw-html", action="store_true", dest="raw_html",
                       help="Outlook only: keep the HTML body; its text is body_text")
    p_get.set_defaults(func=cmd_delegated)

    p_gdraft = sub.add_parser("get-draft", allow_abbrev=False,
                              help="Fetch a draft's to/cc/subject and raw body")
    p_gdraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_gdraft.add_argument("--account", default=None, help=acct_help)
    p_gdraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_gdraft.add_argument("--with-fingerprint", action="store_true", dest="with_fingerprint",
                          help="Gmail only: add the content fingerprint of this draft version")
    p_gdraft.set_defaults(func=cmd_delegated)

    p_mread = sub.add_parser("mark-read", allow_abbrev=False,
                             help="Mark a message read in place; never moves it")
    p_mread.add_argument("--provider", required=True, choices=PROVIDERS)
    p_mread.add_argument("--account", default=None, help=acct_help)
    p_mread.add_argument("--message-id", required=True, dest="message_id")
    p_mread.set_defaults(func=cmd_delegated)

    p_munread = sub.add_parser("mark-unread", allow_abbrev=False,
                               help="Mark a message unread in place; never moves it")
    p_munread.add_argument("--provider", required=True, choices=PROVIDERS)
    p_munread.add_argument("--account", default=None, help=acct_help)
    p_munread.add_argument("--message-id", required=True, dest="message_id")
    p_munread.set_defaults(func=cmd_delegated)

    p_cdraft = sub.add_parser("create-draft", allow_abbrev=False, help="Create a new draft")
    p_cdraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_cdraft.add_argument("--account", default=None, help=acct_help)
    p_cdraft.add_argument("--to", required=True, help="Comma-separated addresses")
    p_cdraft.add_argument("--subject", required=True)
    p_cdraft.add_argument("--body-file", required=True, dest="body_file")
    p_cdraft.add_argument("--cc", default=None)
    p_cdraft.add_argument("--bcc", default=None, help="Gmail only")
    p_cdraft.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                          dest="content_type", help="Outlook only")
    p_cdraft.add_argument("--force-new-thread", action="store_true", dest="force_new_thread",
                          help="Outlook: allow a Re:/Fwd: subject on a genuinely new thread. "
                               "Never to answer a conversation: that is create-reply.")
    p_cdraft.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                          help="A ledger row this draft delivers (repeat for more)")
    p_cdraft.set_defaults(func=cmd_delegated)

    p_crep = sub.add_parser("create-reply", allow_abbrev=False,
                            help="Create a threaded reply draft (reply-all unless --sender-only)")
    p_crep.add_argument("--provider", required=True, choices=PROVIDERS)
    p_crep.add_argument("--account", default=None, help=acct_help)
    target = p_crep.add_mutually_exclusive_group(required=True)
    target.add_argument("--message-id", dest="message_id", help="The message answered")
    target.add_argument("--thread-id", dest="thread_id",
                        help="Gmail only: answer the thread's newest message")
    p_crep.add_argument("--body-file", required=True, dest="body_file")
    p_crep.add_argument("--to", default=None,
                        help="OVERRIDE recipients: the reply keeps the quoted thread but goes "
                             "to these addresses instead of the thread's participants")
    p_crep.add_argument("--subject", default=None, help="Gmail only (default: Re: the original)")
    p_crep.add_argument("--cc", default=None, help="Addresses added to the Cc line")
    p_crep.add_argument("--bcc", default=None, help="Gmail only")
    p_crep.add_argument("--sender-only", action="store_true", dest="sender_only")
    p_crep.add_argument("--force", action="store_true",
                        help="Draft even if the user already sent the latest message in "
                             "the thread")
    p_crep.add_argument("--no-quote", action="store_true", dest="no_quote",
                        help="Gmail only: leave the quoted message out")
    p_crep.add_argument("--conversation-id", default=None, dest="conversation_id",
                        help="Outlook only: the thread, so a stale message id self-heals")
    p_crep.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                        dest="content_type", help="Outlook only")
    p_crep.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                        help="A ledger row this draft delivers (repeat for more)")
    p_crep.set_defaults(func=cmd_delegated)

    p_udraft = sub.add_parser("update-draft", allow_abbrev=False,
                              help="Replace a draft's body in place (recipients kept unless given)")
    p_udraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_udraft.add_argument("--account", default=None, help=acct_help)
    p_udraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_udraft.add_argument("--body-file", required=True, dest="body_file")
    p_udraft.add_argument("--to", default=None)
    p_udraft.add_argument("--cc", default=None)
    p_udraft.add_argument("--subject", default=None, help="Gmail only")
    p_udraft.add_argument("--bcc", default=None, help="Gmail only")
    p_udraft.add_argument("--thread-id", default=None, dest="thread_id",
                          help="Gmail only: move the draft into this thread (answers its newest message)")
    p_udraft.add_argument("--reply-to-message-id", default=None, dest="reply_to_message_id",
                          help="Gmail only: move the draft into that message's thread and quote it")
    p_udraft.add_argument("--no-quote", action="store_true", dest="no_quote",
                          help="Gmail only: drop the quote")
    p_udraft.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                          dest="content_type", help="Outlook only")
    p_udraft.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                          help="A ledger row this draft delivers (repeat for more)")
    p_udraft.set_defaults(func=cmd_delegated)

    p_ddraft = sub.add_parser("delete-draft", allow_abbrev=False, help="Delete a draft")
    p_ddraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_ddraft.add_argument("--account", default=None, help=acct_help)
    p_ddraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_ddraft.set_defaults(func=cmd_delegated)

    p_cfwd = sub.add_parser("create-forward", allow_abbrev=False,
                            help="Forward a message's whole thread, with a note above it")
    p_cfwd.add_argument("--provider", required=True, choices=PROVIDERS)
    p_cfwd.add_argument("--account", default=None, help=acct_help)
    p_cfwd.add_argument("--message-id", required=True, dest="message_id")
    p_cfwd.add_argument("--to", required=True, help="Comma-separated addresses")
    p_cfwd.add_argument("--body-file", required=True, dest="body_file",
                        help="The note above the forwarded thread")
    p_cfwd.add_argument("--cc", default=None)
    p_cfwd.add_argument("--bcc", default=None, help="Gmail only")
    p_cfwd.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                        dest="content_type", help="Outlook only")
    p_cfwd.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                        help="A ledger row this draft delivers (repeat for more)")
    p_cfwd.set_defaults(func=cmd_delegated)

    p_pf = sub.add_parser("preflight-check", allow_abbrev=False,
                          help="Pre-send review of a draft: errors block the send")
    p_pf.add_argument("--provider", required=True, choices=PROVIDERS)
    p_pf.add_argument("--account", default=None, help=acct_help)
    p_pf.add_argument("--draft-id", required=True, dest="draft_id")
    p_pf.set_defaults(func=cmd_delegated)

    p_desc = sub.add_parser("describe-attachment", allow_abbrev=False,
                            help="Read an image on a message as text, inline images included")
    p_desc.add_argument("--provider", required=True, choices=PROVIDERS)
    p_desc.add_argument("--account", default=None, help=acct_help)
    p_desc.add_argument("--message-id", required=True, dest="message_id")
    which = p_desc.add_mutually_exclusive_group(required=True)
    which.add_argument("--attachment-id", dest="attachment_id",
                       help="Exact id from get-message, get-attachments or list-attachments")
    which.add_argument("--name", default=None,
                       help="Case-insensitive substring of the file name (image001 for a "
                            "picture in the body)")
    which.add_argument("--all-images", action="store_true", dest="all_images",
                       help="Every image but the small inline ones (logos), up to six")
    p_desc.add_argument("--question", default="",
                        help="What to read off the image, answered after its text")
    p_desc.add_argument("--output", default=None,
                        help="Also save the one image picked here (not in sealed mail): a "
                             "file under ~/.ea/state, uploads or docs. The way to open it "
                             "when no model reads images.")
    p_desc.set_defaults(func=cmd_describe_attachment)

    p_send = sub.add_parser("send-draft", allow_abbrev=False,
                            help="Send a draft the user approved (the bot's, with its token)")
    p_send.add_argument("--provider", required=True, choices=PROVIDERS)
    p_send.add_argument("--account", default=None, help=acct_help)
    p_send.add_argument("--draft-id", required=True, dest="draft_id")
    p_send.add_argument("--confirm-token", default=None, dest="confirm_token",
                        help="The one-time token the bot mints when the user clicks Send")
    p_send.add_argument("--fingerprint", default=None,
                        help="Gmail only: refuse unless the token names this content")
    p_send.set_defaults(func=cmd_delegated)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    import mail_mediation as _mm_child
    _mediated = _mm_child.check_child_parse(parser, args)  # the verb the mediator checked
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    _mm_child.refuse_unplain_ids("mail_actions", args.command, sys.argv[1:])
    # An owner-side draft edit the mediator did not start holds the draft's lock.
    _mm_child.hold_owner_draft_lock("mail_actions", args.command, sys.argv[1:],
                                    mediated=_mediated)
    args._mediated = _mediated
    return args.func(args)


if __name__ == "__main__":
    import id_handles as _id_handles
    sys.argv[1:] = _id_handles.resolve_argv(sys.argv[1:])
    from mail_mediation import forward_or_none
    forward_or_none("mail_actions", sys.argv[1:], transform=_id_handles.annotate_output)
    # The verb runs in this process: the owner's own action unless the
    # caller (a cron, the mediator for a background lane) set the lane.
    import retry_policy as _retry_policy
    _retry_policy.set_graph_lane(_retry_policy.INTERACTIVE, only_if_unset=True)
    sys.exit(main())
