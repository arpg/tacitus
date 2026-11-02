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

Step 3:
    python3 mail_actions.py create-forward --provider outlook|gmail --message-id <id> --to <a,b> --body-file <f> [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py preflight-check --provider outlook|gmail --draft-id <id>

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
        argv += ["--message-id", args.draft_id, "--raw-html"] if outlook \
            else ["--draft-id", args.draft_id]
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
            if args.thread_id or args.no_quote:
                return [], "--thread-id and --no-quote are Gmail only"
            if not args.message_id:
                return [], "--message-id is required"
            argv += ["--message-id", args.message_id]
            opt("--conversation-id", args.conversation_id)
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
            if not args.sender_only:
                cc = _gmail_reply_cc(acct, args._answered, args.cc)
            opt("--cc", cc)
            if args.no_quote:
                argv.append("--no-quote")
    elif verb == "update-draft":
        if outlook and args.subject:
            return [], "an Outlook draft's subject does not change in update-draft"
        if outlook and (args.reply_to_message_id or args.no_quote):
            return [], "--reply-to-message-id and --no-quote are Gmail only"
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id]
        opt("--to", args.to)
        opt("--cc", args.cc)
        if not outlook:
            opt("--subject", args.subject)
            opt("--reply-to-message-id", args.reply_to_message_id)
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
    if args.command == "create-reply" and args.provider == "gmail":
        # gmail_client has no already-replied skip: make graph_actions's here.
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
    if isinstance(result, dict) and "error" in result and rc == 0:
        rc = 1
    print(json.dumps(result, indent=2))
    return rc


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
    p_crep.add_argument("--cc", default=None, help="Addresses added to the Cc line")
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
