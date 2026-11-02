#!/usr/bin/env python3
"""
mail_actions.py: one mail CLI for both providers (issue #76).

Every verb takes --provider outlook|gmail and, for Gmail, an optional
--account (the first of the profile's gmail_accounts when omitted). The
verbs print mail_client.py's normalized JSON.

Step 1 has the reads:
    python3 mail_actions.py search --provider outlook --query "<q>" [--folder <name>]
    python3 mail_actions.py search --provider gmail --query "is:unread" [--account <addr>]
    python3 mail_actions.py get-message --provider outlook|gmail --message-id <id>
    python3 mail_actions.py get-draft --provider outlook|gmail --draft-id <id>
    python3 mail_actions.py mark-read --provider outlook|gmail --message-id <id>
    python3 mail_actions.py mark-unread --provider outlook|gmail --message-id <id>

Step 2 has the draft writes:
    python3 mail_actions.py create-draft --provider outlook|gmail --to <a,b> --subject <s> --body-file <f> [--cc <a>] [--bcc <a>] [--ledger-id <id>]...
    python3 mail_actions.py create-reply --provider outlook|gmail --message-id <id> --body-file <f> [--sender-only] [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py update-draft --provider outlook|gmail --draft-id <id> --body-file <f> [--to <a>] [--cc <a>] [--ledger-id <id>]...
    python3 mail_actions.py delete-draft --provider outlook|gmail --draft-id <id>

Each write runs the provider CLI's own verb in this process (DELEGATE):
its journaling (draft_events), its checks and create-reply's skip when
the user already replied are the provider verb's own. The unified verb
adds "id", "provider" and "account" to what that verb prints. A reply is
reply-all unless --sender-only, on both providers.

The entry point does what graph_actions.py's does: it resolves a run's
short id handles, and under mail mediation it forwards the call to the
owner-side mediator, which runs it with the token.
"""
import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def cmd_search(args) -> int:
    import mail_client
    results = mail_client.search(
        provider=args.provider, query=args.query,
        account=args.account, max_results=args.max_results, folder=args.folder,
    )
    print(json.dumps(results, indent=2))
    return 0


def cmd_get_message(args) -> int:
    import mail_client
    result = mail_client.get_message(
        provider=args.provider, message_id=args.message_id, account=args.account,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_get_draft(args) -> int:
    import mail_client
    result = mail_client.get_draft(
        provider=args.provider, draft_id=args.draft_id, account=args.account,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_mark_read(args) -> int:
    import mail_client
    result = mail_client.mark_read(
        provider=args.provider, message_id=args.message_id, account=args.account,
    )
    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


def cmd_mark_unread(args) -> int:
    import mail_client
    result = mail_client.mark_unread(
        provider=args.provider, message_id=args.message_id, account=args.account,
    )
    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


# verb -> provider -> (tool, verb): the provider verb a draft write runs.
DELEGATE = {
    "create-draft": {"outlook": ("graph_actions", "create-draft"),
                     "gmail": ("gmail_client", "create-draft")},
    "create-reply": {"outlook": ("graph_actions", "create-reply"),
                     "gmail": ("gmail_client", "create-draft")},
    "update-draft": {"outlook": ("graph_actions", "update-draft"),
                     "gmail": ("gmail_client", "update-draft")},
    "delete-draft": {"outlook": ("graph_actions", "delete-draft"),
                     "gmail": ("gmail_client", "delete-draft")},
}


def _fail(msg: str, code: int = 2) -> int:
    print(json.dumps({"error": msg}, indent=2))
    return code


def _gmail_reply_cc(acct: str, message_id: str, extra: str) -> tuple[str, str]:
    """(cc, error) for a Gmail reply-all: the original's To and Cc and the
    caller's --cc, less the account itself and the sender (who gets the To
    line from gmail_client)."""
    import email.utils as eu
    import gmail_client
    orig = gmail_client.get_message(acct, message_id)
    if "error" in orig:
        return "", orig["error"]
    skip = {acct.lower()} | {a.lower() for _n, a in eu.getaddresses(
        [orig.get("from", ""), orig.get("reply_to", "")]) if a}
    out = []
    for _n, a in eu.getaddresses([orig.get("to", ""), orig.get("cc", ""), extra or ""]):
        if a and a.lower() not in skip and a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return ",".join(out), ""


def _provider_argv(args, acct: str | None) -> tuple[list[str], str]:
    """(the provider verb's argv, error)."""
    verb, outlook = args.command, args.provider == "outlook"
    argv = [DELEGATE[verb][args.provider][1]]
    if not outlook:
        argv += ["--email", acct]

    def opt(flag, value):
        if value:
            argv.extend([flag, value])

    if verb != "delete-draft":
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
    elif verb == "create-reply":
        if outlook:
            argv += ["--message-id", args.message_id]
            opt("--conversation-id", args.conversation_id)
            opt("--cc", args.cc)
            if args.sender_only:
                argv.append("--sender-only")
        else:
            argv += ["--reply-to-message-id", args.message_id]
            cc = args.cc
            if not args.sender_only:
                cc, err = _gmail_reply_cc(acct, args.message_id, args.cc)
                if err:
                    return [], err
            opt("--cc", cc)
    elif verb == "update-draft":
        if outlook and args.subject:
            return [], "an Outlook draft's subject does not change in update-draft"
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id]
        opt("--to", args.to)
        opt("--cc", args.cc)
        if not outlook:
            opt("--subject", args.subject)
    else:
        argv += ["--message-id" if outlook else "--draft-id", args.draft_id]
    if outlook and verb != "delete-draft" and args.content_type:
        argv += ["--content-type", args.content_type]
    return argv, ""


def cmd_draft_write(args) -> int:
    """create-draft, create-reply, update-draft, delete-draft."""
    import mail_client
    acct = None
    if args.provider == "gmail":
        acct = mail_client._resolve_gmail_account(args.account)
        if not acct:
            return _fail("No Gmail account configured")
    if args.command != "delete-draft":
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as e:
            return _fail(f"cannot read --body-file: {e}")
        if not body.strip():
            return _fail("the body is empty: write the text to --body-file")
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
                graph_actions._resolve_body(pargs)
                pargs.func(pargs)
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
    if isinstance(result, dict):
        result.setdefault("id", result.get("draft_id") or result.get("message_id") or "")
        result["provider"], result["account"] = args.provider, acct
        if "error" in result and rc == 0:
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
    p_search.add_argument("--max-results", type=int, default=20, dest="max_results")
    p_search.add_argument("--folder", default=None,
                          help="Outlook only: search inside one folder (inbox, sentitems, "
                               "archive, or a folder's own name). Gmail: label:<name> in --query.")
    p_search.set_defaults(func=cmd_search)

    p_get = sub.add_parser("get-message", allow_abbrev=False, help="Fetch a message with its body")
    p_get.add_argument("--provider", required=True, choices=PROVIDERS)
    p_get.add_argument("--account", default=None, help=acct_help)
    p_get.add_argument("--message-id", required=True, dest="message_id")
    p_get.set_defaults(func=cmd_get_message)

    p_gdraft = sub.add_parser("get-draft", allow_abbrev=False,
                              help="Fetch a draft's to/cc/subject and raw body")
    p_gdraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_gdraft.add_argument("--account", default=None, help=acct_help)
    p_gdraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_gdraft.set_defaults(func=cmd_get_draft)

    p_mread = sub.add_parser("mark-read", allow_abbrev=False,
                             help="Mark a message read in place; never moves it")
    p_mread.add_argument("--provider", required=True, choices=PROVIDERS)
    p_mread.add_argument("--account", default=None, help=acct_help)
    p_mread.add_argument("--message-id", required=True, dest="message_id")
    p_mread.set_defaults(func=cmd_mark_read)

    p_munread = sub.add_parser("mark-unread", allow_abbrev=False,
                               help="Mark a message unread in place; never moves it")
    p_munread.add_argument("--provider", required=True, choices=PROVIDERS)
    p_munread.add_argument("--account", default=None, help=acct_help)
    p_munread.add_argument("--message-id", required=True, dest="message_id")
    p_munread.set_defaults(func=cmd_mark_unread)

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
    p_cdraft.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                          help="A ledger row this draft delivers (repeat for more)")
    p_cdraft.set_defaults(func=cmd_draft_write)

    p_crep = sub.add_parser("create-reply", allow_abbrev=False,
                            help="Create a threaded reply draft (reply-all unless --sender-only)")
    p_crep.add_argument("--provider", required=True, choices=PROVIDERS)
    p_crep.add_argument("--account", default=None, help=acct_help)
    p_crep.add_argument("--message-id", required=True, dest="message_id")
    p_crep.add_argument("--body-file", required=True, dest="body_file")
    p_crep.add_argument("--cc", default=None, help="Addresses added to the Cc line")
    p_crep.add_argument("--sender-only", action="store_true", dest="sender_only")
    p_crep.add_argument("--conversation-id", default=None, dest="conversation_id",
                        help="Outlook only: the thread, so a stale message id self-heals")
    p_crep.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                        dest="content_type", help="Outlook only")
    p_crep.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                        help="A ledger row this draft delivers (repeat for more)")
    p_crep.set_defaults(func=cmd_draft_write)

    p_udraft = sub.add_parser("update-draft", allow_abbrev=False,
                              help="Replace a draft's body in place (recipients kept unless given)")
    p_udraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_udraft.add_argument("--account", default=None, help=acct_help)
    p_udraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_udraft.add_argument("--body-file", required=True, dest="body_file")
    p_udraft.add_argument("--to", default=None)
    p_udraft.add_argument("--cc", default=None)
    p_udraft.add_argument("--subject", default=None, help="Gmail only")
    p_udraft.add_argument("--content-type", default=None, choices=["Text", "HTML"],
                          dest="content_type", help="Outlook only")
    p_udraft.add_argument("--ledger-id", action="append", default=[], dest="ledger_id",
                          help="A ledger row this draft delivers (repeat for more)")
    p_udraft.set_defaults(func=cmd_draft_write)

    p_ddraft = sub.add_parser("delete-draft", allow_abbrev=False, help="Delete a draft")
    p_ddraft.add_argument("--provider", required=True, choices=PROVIDERS)
    p_ddraft.add_argument("--account", default=None, help=acct_help)
    p_ddraft.add_argument("--draft-id", required=True, dest="draft_id")
    p_ddraft.set_defaults(func=cmd_draft_write)
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
    sys.exit(main())
