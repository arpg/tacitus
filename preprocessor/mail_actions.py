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

The entry point does what graph_actions.py's does: it resolves a run's
short id handles, and under mail mediation it forwards the call to the
owner-side mediator, which runs it with the token.
"""
import argparse
import json
import sys
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
    return args.func(args)


if __name__ == "__main__":
    import id_handles as _id_handles
    sys.argv[1:] = _id_handles.resolve_argv(sys.argv[1:])
    from mail_mediation import forward_or_none
    forward_or_none("mail_actions", sys.argv[1:], transform=_id_handles.annotate_output)
    sys.exit(main())
