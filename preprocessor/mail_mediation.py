#!/usr/bin/env python3
"""Bot-mediated mail/calendar ops: keep send + the raw tokens out of the resident.

The resident EA and the scheduled agents run as the ``ea-bot`` user (the
prompt-injectable surface). This module is the IN half of the split (see
docs/mail-mediation-spec.md):

  * SAFE verbs (read / draft / reversible inbox state) are forwarded to a
    owner-side service (mail_mediator.py) that holds the tokens and executes them.
  * GATED verbs (send / calendar mutate) are refused for ea-bot outright; they
    run only owner-side after a Slack button.

Each client (graph_actions.py, gmail_client.py, google_calendar_client.py) calls
``forward_or_none(...)`` as the first line of ``main()``. When mediation applies
it handles the call end-to-end (enqueue -> poll -> print -> exit) and NEVER
returns; otherwise it returns None and the client runs directly (the owner path, or
the whole feature disabled).

Trigger (``should_mediate``): ONLY for the ea-bot euid. The owner side never
mediates, it IS the mediator. ``EA_FORCE_MEDIATE=1`` forces it on for testing.

The off-switch, and why it is not a config flag
-----------------------------------------------
``ea-config.json`` is ea-bot-owned, so the resident could disable its own gate
by writing one brace. The resident's own config therefore does not decide. For
the ea-bot euid:

1. ``EA_FORCE_MEDIATE=1``            -> mediate (test hook).
2. An owner/root-owned off-switch       -> do not mediate. Either
   ``$EA_DIR/auth/mediation-disabled`` exists, or ``ea-config.json`` says
   ``enabled: false``, but EITHER is honoured only when the file AND its
   directory are un-writable by the current euid. An off-switch the resident
   can write is a bypass, not an off-switch.
3. ``MEDIATOR_UNIT`` exists          -> mediate. The systemd unit lives in
   root-owned ``/etc/systemd/system``; ea-bot can neither create nor remove it,
   so its presence is un-forgeable evidence that this host runs mediation.
   An unreadable/malformed/absent config CANNOT turn that off (fail closed): if
   the mediator is down, SAFE verbs time out. A mail outage is the accepted
   failure mode; an open gate is not.
4. Otherwise                         -> honour ``enabled: true`` from the
   config. On a host with no mediator installed there is nothing to forward to
   and ea-bot holds the tokens anyway, so running direct is not a bypass.

To turn mediation off on a mediated host, use the owner-owned kill file:
``sudo -u <owner> touch ~ea-bot/.ea/auth/mediation-disabled`` (the auth directory is
owner-owned 0750, so ea-bot cannot create or remove entries in it), or remove the
systemd unit. ``enable-mail-mediation.sh --rollback`` flips the config flag,
which is advisory.
"""
from __future__ import annotations

import ast
import json
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
QUEUE = EA_DIR / "state" / "mail-queue"
CONFIG = EA_DIR / "config" / "ea-config.json"

# Root-owned evidence that this host runs mediation. ea-bot cannot create or
# remove it (/etc/systemd/system is root:root 0755), which is what makes it a
# trustworthy on-switch where the ea-bot-owned config file is not.
# The root-owned service definition whose presence means "mediated, whatever
# the ea-bot-writable config says". On macOS the mediator is a LaunchDaemon
# (deploy/macos/install-services.sh); without this, the decision there fell
# through to the config flag, which ea-bot can set to false.
def _mediator_unit() -> Path:
    if sys.platform != "darwin":
        return Path("/etc/systemd/system/ea-mail-mediator.service")
    try:
        import launchd_labels
    except ImportError:
        from preprocessor import launchd_labels
    return launchd_labels.plist_path(launchd_labels.label("ea-mail-mediator"))


MEDIATOR_UNIT = _mediator_unit()
RESIDENT_USER = "ea-bot"

POLL_S = 0.35
TIMEOUT_S = 150  # ea-bot side: how long to wait for the owner-side service to answer

# ---- verb classification (validated against a month of production logs) ----
SAFE = {
    "graph_actions": {
        "search-mail", "list-mail", "get-message", "get-conversation",
        "get-event",
        "calendar-view", "resolve-contact",
        "get-attachments", "download-attachment", "scan-attachments",
        "get-gmail-draft",
        "create-draft", "create-reply", "update-draft", "delete-draft",
        "preflight-check", "attach-file",
        # Removes one attachment from a DRAFT; graph_actions refuses any
        # message that is not a draft, so mail received or sent keeps its
        # files.
        "remove-attachment",
        "archive-message", "archive-conversation", "snooze-message",
        # The read flag only, in place.
        "mark-read",
        # Writes the categories list on one message: a label, nothing
        # leaves the mailbox.
        "set-categories",
        "wake-snoozed", "create-todo",
        # Releases ONE Outlook event that the EA itself placed as a hold:
        # graph_client.clear_hold deletes only an event carrying the "EA
        # Hold" category, shown as tentative, with no attendees, organised
        # by the user. Anything else is refused there, owner-side, so this is
        # not a route around the gated delete-event: a real meeting cannot
        # meet the check, and nothing it can delete notifies anyone.
        "clear-hold",
    },
    "gmail_client": {
        "search", "get-message", "get-thread", "get-draft",
        "create-draft", "update-draft", "delete-draft",
        "mark-read",
        # Adds files to an existing draft (the Gmail API has no attachment
        # endpoint, so the draft is rebuilt in place). --file is a
        # FILE_FLAGS input: staged through the owner-private directory and
        # confined to the allowed roots, as for the Outlook twin.
        "attach-file",
        # The reverse, on a draft too: the draft is rebuilt without the
        # named file part.
        "remove-attachment",
        # Reads of message content the Gmail MCP cannot fetch. The
        # --output path on download-attachment goes through OUTPUT_FLAGS
        # staging like its graph_actions twin, so the owner-side write is
        # confined to the allowed roots.
        "list-attachments", "download-attachment",
        # The account's recent drafts (ids and times): how the bot finds a
        # draft the Gmail MCP created, since the MCP journals nothing.
        "list-drafts",
    },
    # The unified mail CLI (issue #76): reads on either provider.
    "mail_actions": {"search", "get-message", "get-draft", "mark-read"},
    # list-ea-managed is a READ of the EA's own rule calendar; it returns the
    # event ids a later move or delete has to name. It grants no write:
    # create-event stays GATED apart from the attendee-less auto-hold
    # carve-out below.
    "google_calendar_client": {"get-events", "list-ea-managed"},
    # The solver only READS calendars (and writes a local decisions file); no
    # external send/mutate. Mediated so the resident (ea-bot, no token) can run
    # it: the owner-side mediator executes it with the token. Exactly the
    # subcommands scheduling_engine.py's argparse defines, since a verb with no
    # subparser dies in the mediator's argparse with exit 2.
    "scheduling_engine": {
        "propose-meeting", "validate-draft", "log-decision",
        "review-patterns",
        # Free/busy of named colleagues over one window (Graph getSchedule,
        # a read) plus the user's own events in it. The question the solver
        # cannot answer: "can these people make THIS slot", including a
        # slot the user already holds.
        "check-attendees",
        # One call for a reschedule request: the thread, the meeting(s) on
        # the calendar with that sender, the sender's history with the
        # user, solver options ranked by that history, and a recommendation.
        # Reads only (it runs propose-meeting and validate-draft under the
        # hood); the move itself still goes through the Reschedule button.
        "reschedule-brief",
    },
    # The tailnet route for an EA-built deck. `serve` and `off` are
    # tailscaled writes only the operator (the owner) may make, so they run
    # owner-side; deck_serve.py re-validates there (EA port range, the
    # listener must be ea-bot's, the path must not be another user's, no
    # Funnel on the node). There is no funnel verb anywhere, so a route
    # created this way is tailnet-only by construction.
    "deck_serve": {"serve", "off", "status"},
    # The remote email writer. It reads the brief the resident staged, the
    # thread and the draft (reads the resident has already), sends them to
    # the remote endpoint the owner approved for all mail, and writes the
    # body back into the resident's state. Nothing leaves the mailbox, and
    # the endpoint key stays owner-side.
    "email_writer": {"write"},
    # A plugin card's one-time click token (plugin_cards.py). The bot mints
    # it into the owner-private directory after a click; a resident
    # executor's check comes here, and the mediator takes the token as the
    # owner. The verb reads and rewrites one owner-private file and
    # nothing else, so a resident that forges a token file in its own space
    # changes nothing.
    "plugin_cards": {"consume-token"},
}
GATED = {
    "graph_actions": {
        "send-draft", "send-gmail-draft", "schedule-send",
        "request-confirm",
        "create-event", "update-event", "delete-event",
    },
    "gmail_client": {"send-draft"},
    # All three write the user's own Google calendars when the bot runs them
    # with a button token. The resident reaches them only through the
    # EA-Managed carve-out below.
    "google_calendar_client": {"create-event", "update-event", "delete-event"},
}

# The SAFE verbs that change an existing draft, and the option that names
# the draft. The mediator holds that draft's lock (draft_lock.py) while the
# verb runs, and a send holds it from its content check to the send, so no
# edit lands between the two. None: the verb finds its draft itself
# (create-reply revises the EA's draft in the thread), so it holds the whole
# mailbox. tests/test_send_content_binding.py keeps this table in step with
# the verbs whose code writes a draft.
DRAFT_EDIT_VERBS = {
    "graph_actions": {
        "update-draft": "--message-id", "attach-file": "--message-id",
        "remove-attachment": "--message-id", "delete-draft": "--message-id",
        "create-reply": None,
    },
    "gmail_client": {
        "update-draft": "--draft-id", "attach-file": "--draft-id",
        "remove-attachment": "--draft-id", "delete-draft": "--draft-id",
    },
    # No draft-changing verb yet: the unified writes come in #76 step 2.
    "mail_actions": {},
}


def draft_lock_scope(tool: str, verb: str, argv: list[str]):
    """None when the verb changes no draft, else (draft ids, whole mailbox)
    for draft_lock.hold. Every value the naming option carries is locked;
    a draft-changing verb with no such value holds the whole mailbox."""
    verbs = DRAFT_EDIT_VERBS.get(tool) or {}
    if verb not in verbs:
        return None
    flag = verbs[verb]
    if flag is None:
        return [], True
    ids = []
    for i, a in enumerate(argv or []):
        if a == flag and i + 1 < len(argv):
            ids.append(str(argv[i + 1]))
        elif a.startswith(flag + "="):
            ids.append(a[len(flag) + 1:])
    ids = [x for x in ids if x.strip()]
    return (ids, False) if ids else ([], True)

# File-path flags whose value must stay inside ea-bot's own dirs, so a
# owner-executed op cannot be steered to read/write owner or system files.
FILE_FLAGS = {"--output", "--body-file", "--path", "--file", "--screenshot",
              "--attachment", "--out", "--dest", "--input", "--brief-file"}
# The subset the CHILD WRITES (everything else it reads). The mediator stages
# both directions through an owner-private directory rather than handing the child
# an ea-bot-controlled path at all; see mail_mediator._stage_paths. Only
# `--output` (download-attachment, graph_actions and gmail_client) exists on a
# mediated CLI today; the rest are forward-looking, and an over-broad list is
# inert.
OUTPUT_FLAGS = {"--output", "--out", "--dest", "--screenshot"}

# ---- the options each mediated verb's parser defines ------------------------
# FILE_FLAGS match by exact spelling, so a spelling argparse reads as a file
# option and this module does not (an abbreviation such as --body-f, an alias,
# a short form) would skip path confinement. Every mediated parser sets
# allow_abbrev=False, and sanitize() refuses any option token that is not an
# exact option of the verb's own parser. The option set is read from the
# source of the tool the request names, beside this module: the file the
# owner-side mediator executes, so the set cannot drift from the parser.
# tests/test_mail_mediation.py checks the set against the parser argparse
# builds.
_TOOL_DIR = Path(__file__).resolve().parent
_PARSER_FUNCS = {"ArgumentParser", "add_parser"}
_GROUP_FUNCS = {"add_argument_group", "add_mutually_exclusive_group"}
# argparse reads a one-dash token that looks like this as a value.
_NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")
_OPTIONS_CACHE: dict[str, tuple[tuple, tuple[dict, dict]]] = {}
# The argparse actions that keep every use of their option. Any other action
# keeps the last use only, while a check that reads argv may read the first:
# so a repeat of such an option is refused (option_error).
_REPEATABLE_ACTIONS = {"append", "append_const", "extend", "count"}


def _str_literal(node, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


class _ParserScan(ast.NodeVisitor):
    """Collect {verb: {option spelling: every spelling of that option}} from
    the add_subparsers / add_parser / add_argument calls in one module.

    A verb whose options are not all string literals (a computed flag name,
    parents=, **kwargs) lands in `unreadable`, and the caller refuses it."""

    def __init__(self, consts: dict[str, str]):
        self.consts = consts
        self.verbs: dict[str, dict[str, frozenset]] = {}
        # the spellings of the options argparse lets repeat (_REPEATABLE)
        self.repeatable: dict[str, set[str]] = {}
        self.unreadable: set[str] = set()
        # one (parser variable -> verbs, subparsers variables) per scope
        self._scopes: list[tuple[dict[str, tuple], set[str]]] = [({}, set())]

    def visit_FunctionDef(self, node):
        self._scopes.append(({}, set()))
        self.generic_visit(node)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _receiver_verbs(self, node) -> tuple:
        """The verbs a parser expression stands for, () when it is none."""
        if isinstance(node, ast.Name):
            return self._scopes[-1][0].get(node.id, ())
        if isinstance(node, ast.Call):
            return self._add_parser_verbs(node)
        return ()

    def _add_parser_verbs(self, call) -> tuple:
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "add_parser"
                and isinstance(f.value, ast.Name)
                and f.value.id in self._scopes[-1][1]):
            return ()
        names = [_str_literal(call.args[0], self.consts)] if call.args else []
        add_help = True
        bad = False
        for kw in call.keywords:
            if kw.arg is None or kw.arg == "parents":
                bad = True
            elif kw.arg == "aliases":
                if isinstance(kw.value, (ast.List, ast.Tuple)):
                    names += [_str_literal(e, self.consts) for e in kw.value.elts]
                else:
                    bad = True
            elif kw.arg == "add_help":
                add_help = not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        if not names or None in names:
            return ()
        for verb in names:
            if verb not in self.verbs:
                self.verbs[verb] = {}
                if add_help:
                    self._add(verb, ("-h", "--help"))
            if bad:
                self.unreadable.add(verb)
        return tuple(names)

    def _add(self, verb: str, spellings):
        group = frozenset(spellings)
        for s in spellings:
            self.verbs[verb][s] = group

    def visit_Assign(self, node):
        self.generic_visit(node)
        parsers, subs = self._scopes[-1]
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        value = node.value
        verbs: tuple = ()
        is_subs = False
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            attr = value.func.attr
            if attr == "add_subparsers":
                is_subs = True
            elif attr == "add_parser":
                verbs = self._add_parser_verbs(value)
            elif attr in _GROUP_FUNCS:
                verbs = self._receiver_verbs(value.func.value)
        for name in targets:
            subs.discard(name)
            parsers.pop(name, None)
            if is_subs:
                subs.add(name)
            elif verbs:
                parsers[name] = verbs

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "add_parser":
            self._add_parser_verbs(node)
        elif isinstance(f, ast.Attribute) and f.attr == "add_argument":
            verbs = self._receiver_verbs(f.value)
            if verbs:
                flags = [_str_literal(a, self.consts) for a in node.args]
                bad = (None in flags or any(isinstance(a, ast.Starred) for a in node.args)
                       or any(kw.arg is None for kw in node.keywords))
                spellings = [s for s in flags if s and s.startswith("-")]
                action = next((kw.value for kw in node.keywords if kw.arg == "action"), None)
                # Only a literal action names a repeatable one; any other
                # action (store, store_true, a class) keeps its last value,
                # so a repeat of it is refused.
                repeats = (isinstance(action, ast.Constant)
                           and action.value in _REPEATABLE_ACTIONS)
                for verb in verbs:
                    if bad:
                        self.unreadable.add(verb)
                    elif spellings:
                        self._add(verb, spellings)
                        if repeats:
                            self.repeatable.setdefault(verb, set()).update(spellings)
        self.generic_visit(node)


def _module_constants(tree) -> dict[str, str]:
    consts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    return consts


def _scan_parsers_full(source: str) -> tuple[dict, dict]:
    """({verb: options or None}, {verb: repeatable spellings}) for one module."""
    tree = ast.parse(source)
    scan = _ParserScan(_module_constants(tree))
    scan.visit(tree)
    opts = {verb: (None if verb in scan.unreadable else o)
            for verb, o in scan.verbs.items()}
    return opts, {verb: frozenset(s) for verb, s in scan.repeatable.items()}


def _scan_parsers(source: str) -> dict:
    return _scan_parsers_full(source)[0]


def _tool_scan(tool: str) -> tuple[dict, dict] | None:
    """The parser scan of a core tool's source, cached by file identity."""
    if tool not in SAFE and tool not in GATED:
        return None
    path = _TOOL_DIR / f"{tool}.py"
    try:
        st = path.stat()
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        cached = _OPTIONS_CACHE.get(tool)
        if cached is None or cached[0] != key:
            cached = (key, _scan_parsers_full(path.read_text()))
            _OPTIONS_CACHE[tool] = cached
    except (OSError, SyntaxError, ValueError):
        return None
    return cached[1]


def parser_options(tool: str, verb: str) -> dict[str, frozenset] | None:
    """{option spelling: all spellings of that option} for one core verb, or
    None when the tool or verb is unknown or its parser cannot be read."""
    scan = _tool_scan(tool)
    return None if scan is None else scan[0].get(verb)


def repeatable_options(tool: str, verb: str) -> frozenset:
    """The spellings of the options of one core verb that argparse lets
    repeat (append, append_const, extend, count). Every other option keeps
    only its last value, so option_error refuses a second use of it."""
    scan = _tool_scan(tool)
    return frozenset() if scan is None else scan[1].get(verb, frozenset())


def _option_name(token: str) -> str | None:
    """The option argparse would look up for one argv token, or None when it
    reads the token as a value."""
    if not token.startswith("-") or token == "-":
        return None
    name = token.partition("=")[0]
    if " " in name:
        return None                      # argparse reads it as a value
    if not token.startswith("--") and _NEGATIVE_NUMBER.match(token):
        return None
    return name


def option_error(tool: str, verb: str, argv: list[str]) -> str | None:
    """Refuse any option token that is not an exact option of the verb's
    parser, any spelling of a file option other than its FILE_FLAGS one, and
    a second use of any option that is not an append option of that parser
    (argparse acts on the last use, a check may read another). Refused,
    never rewritten.

    The request's verb field is what the allowlist checked, and argparse
    takes the subcommand from argv, so argv[0] must be that verb exactly.
    Then nothing comes before the subcommand, so no option of the tool's
    top-level parser can be set, and every later token goes to the verb's
    own subparser."""
    if not verb or verb.startswith("-") or not argv or argv[0] != verb:
        first = argv[0] if argv else ""
        return (f"argv starts with {first!r}, not the verb {verb!r} the request "
                f"declares")
    if is_plugin_tool(tool):
        # The plugin's parser is not readable here. An abbreviation of a file
        # flag is a strict prefix of it, so that is what is refused.
        for token in argv[1:]:
            name = _option_name(token)
            if name and name.startswith("--") and name not in FILE_FLAGS and any(
                    f.startswith(name) for f in FILE_FLAGS):
                return (f"option {name!r} abbreviates a file option; "
                        f"spell file options in full")
        return None
    opts = parser_options(tool, verb)
    if opts is None:
        return f"cannot read the options of '{tool} {verb}' from its parser"
    repeatable = repeatable_options(tool, verb)
    seen: set[frozenset] = set()
    for token in argv[1:]:
        name = _option_name(token)
        if name is None:
            continue
        spellings = opts.get(name)
        if spellings is None:
            return (f"{name!r} is not an option of '{tool} {verb}' "
                    f"(options are spelled in full)")
        if name not in FILE_FLAGS and spellings & FILE_FLAGS:
            return (f"{name!r} names a file; spell it "
                    f"{sorted(spellings & FILE_FLAGS)[0]}")
        # argparse keeps the last use of a plain option; a check that reads
        # argv could read another one. One use each, unless it appends.
        if spellings in seen and not (spellings & repeatable):
            return (f"{name!r} is given more than once; '{tool} {verb}' takes "
                    f"one value for it")
        seen.add(spellings)
    return None


# The verb the mediator checked, in the environment of the child it starts.
# The child's own parser then confirms it resolved that verb
# (check_child_parse), so the check runs on the real parser at the point
# that executes as the owner.
MEDIATED_VERB_ENV = "EA_MEDIATED_VERB"


def check_child_parse(parser, args) -> bool:
    """Call right after parse_args() in a mediated tool. In a child the
    mediator started, exit 2 unless argparse resolved the verb the mediator
    checked and left every top-level option at its default. Elsewhere it
    does nothing. The variable is removed after the check, so a process this
    one starts is not held to its verb. Returns True in a mediated child
    (the mediator holds its draft lock), else False."""
    want = os.environ.pop(MEDIATED_VERB_ENV, None)
    if want is None:
        return False
    import argparse
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    got = getattr(args, subs[0].dest, None) if subs else None
    own = set()
    if subs and got in subs[0].choices:
        own = {a.dest for a in subs[0].choices[got]._actions}
    changed = [a.dest for a in parser._actions
               if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))
               and a.dest not in own and getattr(args, a.dest, a.default) != a.default]
    if got != want or changed:
        print(json.dumps({"status": "error", "message": (
            f"mediator refused: it checked '{want}' and the parser resolved {got!r}"
            + (f" with top-level options {changed}" if changed else ""))}),
            file=sys.stderr)
        sys.exit(2)
    return True


# The draft lock an owner-side CLI run holds (hold_owner_draft_lock). It is
# released when the process exits and its descriptors close.
_OWNER_CLI_LOCK = None


def hold_owner_draft_lock(tool: str, verb: str, argv: list[str], *, mediated: bool) -> None:
    """Call right after check_child_parse() in graph_actions, gmail_client and
    mail_actions.
    An owner-side run of a draft-changing verb that the mediator did not
    start (the bot, the remote briefing, a cron) takes the same draft lock
    the mediator takes for the resident (draft_lock_scope), and holds it
    until the process exits, so no owner-side edit lands between a send's
    content check and the send. Busy: exit 3 with nothing changed. A
    mediated child (the mediator holds the lock) and the resident's side
    (owner_side() is False) take none."""
    global _OWNER_CLI_LOCK
    if mediated or _OWNER_CLI_LOCK is not None or not owner_side():
        return
    scope = draft_lock_scope(tool, verb, argv)
    if scope is None:
        return
    try:
        import draft_lock
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import draft_lock
    held = draft_lock.hold(scope[0], whole_mailbox=scope[1])
    try:
        held.__enter__()
    except draft_lock.Busy:
        print(json.dumps({"status": "error", "message": (
            "the draft is being sent; nothing was changed. Try again in a minute.")}),
            file=sys.stderr)
        sys.exit(3)
    _OWNER_CLI_LOCK = held


def _allowed_roots() -> list[Path]:
    # Every root here is a directory ea-bot already owns and writes at will, so
    # an owner-executed write into one grants the resident nothing it could not do
    # itself. What stays out is the point: auth/ (the owner-owned tokens),
    # config/, and everything outside EA_DIR.
    # research/ is the folder the owner and the EA both write (the owner's
    # research tree symlinks into it), so a letter or figure kept there can
    # go on a draft without a copy. exports/ holds the EA's own screenshots
    # and exports, and the deck workspace (the deck plugin: EA_DECKS_ROOT, else
    # <resident home>/presentations) holds the PDFs it builds: a poster or
    # a deck goes on a draft from where it was built, since the lane has no
    # copy command.
    decks = Path(os.environ.get("EA_DECKS_ROOT") or (EA_DIR.parent / "presentations"))
    return [EA_DIR / "uploads", EA_DIR / "receipts", EA_DIR / "docs",
            EA_DIR / "research", EA_DIR / "state", EA_DIR / "exports", decks, Path("/tmp")]


# Owner-zone plugin verbs travel as tool "plugin:<name>" (plugin_verbs.py,
# docs/plugins.md). Their class comes from the owner's plugin lock, which
# only an owner process can read. The resident's shim forwards them, and
# the mediator, an owner process, decides.
PLUGIN_TOOL_PREFIX = "plugin:"


def is_plugin_tool(tool: str) -> bool:
    return isinstance(tool, str) and tool.startswith(PLUGIN_TOOL_PREFIX)


def _plugin_verb_class(tool: str, verb: str) -> str | None:
    """None for a core tool. For a plugin tool: "forward" in a resident
    process, else the verb's class in the owner's lock ("safe" or "gated"),
    or "" when the lock does not declare it."""
    if not is_plugin_tool(tool):
        return None
    try:
        import owner_trust
        if not owner_trust.is_owner_process():
            return "forward"
        import plugin_registry
        spec = plugin_registry.owner_verb(tool[len(PLUGIN_TOOL_PREFIX):], verb)
    except Exception:
        return ""
    return str((spec or {}).get("class") or "")


def is_safe(tool: str, verb: str) -> bool:
    cls = _plugin_verb_class(tool, verb)
    if cls is not None:
        return cls in ("safe", "forward")
    return verb in SAFE.get(tool, set())


def is_gated(tool: str, verb: str) -> bool:
    cls = _plugin_verb_class(tool, verb)
    if cls is not None:
        return cls == "gated"
    return verb in GATED.get(tool, set())


# --- Scoped un-gated exception: EA-Managed calendar auto-holds --------------
# The resident may create attendee-less events on ONE dedicated calendar
# WITHOUT a Slack button (a personal hold that emails no one). Every other
# calendar write -- the primary calendar, or anything carrying attendees --
# stays GATED (button-required), so this does not broaden what the EA can do to
# the user's real calendar. Enforced here (the trust boundary) on BOTH the
# ea-bot shim and the owner-side mediator.
#
# Which calendar that is comes from the owner-private <private_dir>/calendars.json
# ({"ea_managed_calendar_id": "<calendar-id>@group.calendar.google.com",
#   "ea_managed_account": "<the Google account that holds it>"}), written by
# `owner_trust.py seed --calendar` from the values the installer put in
# scheduling-preferences.json. The config file is the resident's own, so an
# owner process never takes the id from it: the resident could otherwise point
# the un-gated path at the user's real calendar. Only a process known to be
# the resident (its euid, or a scratch EA tree in a test) uses the config
# value, as a hint for its own pre-check; the mediator decides.
CALENDARS_FILE = "calendars.json"
# Set in the environment of a child the mediator starts. It only ever moves
# a process to the owner side, which fails closed, so anyone may set it.
OWNER_SIDE_ENV = "EA_CALENDAR_OWNER_SIDE"


def owner_side() -> bool:
    """True unless this process is known to be the resident's side: its euid
    is the resident user, or it runs on a scratch EA tree (a test) and is not
    an owner process. An owner process that owner_trust.is_owner_process()
    misclassifies (a private directory it cannot lstat, a single-home
    layout) runs on the install tree, so it lands here, on the side that
    reads only the owner-private file. An unknown answer counts as owner
    side."""
    if os.environ.get(OWNER_SIDE_ENV) == "1":
        return True
    try:
        import owner_trust
        if owner_trust.is_owner_process():
            return True
        if _is_resident():
            return False
        return (os.path.realpath(owner_trust.ea_dir())
                == os.path.realpath(owner_trust.install_ea_dir()))
    except Exception:
        return True


def owner_calendars() -> dict:
    """The owner-private calendars.json, or {} when it is missing or fails
    the owner checks (then no calendar write is un-gated)."""
    import owner_trust
    path = owner_trust.private_dir() / CALENDARS_FILE
    try:
        data = json.loads(owner_trust.read_owner_file(path))
    except FileNotFoundError:
        return {}
    except (owner_trust.OwnerTrustError, OSError, ValueError) as e:
        owner_trust._warn_once(f"{path} refused, no calendar write is un-gated: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def owner_ea_managed_calendar_id() -> str:
    """The EA-Managed calendar id from the owner-private calendars.json, and
    nothing else: "" without it. Every owner-side check uses this."""
    return str(owner_calendars().get("ea_managed_calendar_id") or "").strip()


def ea_managed_calendar_id() -> str:
    """The one Google calendar the resident may write without a button, or
    "" when none is configured (then no calendar write is un-gated).

    Owner side (owner_side()): the owner-private calendars.json, and nothing
    else. Resident side: the id in the resident's scheduling-preferences.json,
    else EA_MANAGED_CALENDAR_ID from the environment (tests)."""
    if owner_side():
        return owner_ea_managed_calendar_id()
    try:
        cfg = json.loads((EA_DIR / "config" / "scheduling-preferences.json").read_text())
        ea = cfg.get("ea_managed_calendar") if isinstance(cfg, dict) else None
        hint = str((ea or {}).get("calendar_id") or "").strip() if isinstance(ea, dict) else ""
    except (OSError, ValueError):
        hint = ""
    return hint or os.environ.get("EA_MANAGED_CALENDAR_ID", "").strip()


def _flag_value(argv: list[str], name: str) -> str | None:
    """The value of the LAST use of `name`, the one argparse keeps, or None.
    option_error also refuses a repeated option that is not an append
    option, so a checked request carries one value for such a flag."""
    found = None
    for i, a in enumerate(argv):
        if a == name:
            found = argv[i + 1] if i + 1 < len(argv) else ""
        elif a.startswith(name + "="):
            found = a[len(name) + 1:]
    return found


# The three google verbs that may touch the EA-Managed calendar un-gated.
# create-event additionally must carry no attendees; update/delete take none.
_EA_MANAGED_VERBS = {"create-event", "update-event", "delete-event"}


def is_ea_managed_autohold(tool: str, verb: str, argv: list[str], *,
                           owner: bool = False) -> bool:
    """True for a google create/update/delete-event that targets ONLY the
    EA-Managed calendar, the calendar writes allowed un-gated.

    These verbs are not simply added to SAFE, because SAFE takes no argv and
    would let the verb name alone authorise a write to the user's real
    calendar. The check stays here, where the calendar id is visible, so the
    boundary is enforced on the ea-bot shim AND the owner-side mediator.
    google_calendar_client._require_ea_managed refuses every other calendar a
    third time. The mediator passes `owner`, so its check reads the
    owner-private id whatever this process looks like.
    """
    if tool != "google_calendar_client" or verb not in _EA_MANAGED_VERBS:
        return False
    # A confirm token selects the client's button path, which writes the
    # account's own calendar. Only the bot mints one, and the resident never
    # carries one.
    if _flag_value(argv, "--confirm-token") is not None:
        return False
    ea = owner_ea_managed_calendar_id() if owner else ea_managed_calendar_id()
    if not ea:
        # No calendar is configured on this side: nothing is un-gated.
        return False
    cal = _flag_value(argv, "--calendar-id")
    if verb == "create-event":
        # create-event defaults to the account's PRIMARY calendar, so the
        # EA-Managed id has to be named for the write to be a hold at all.
        if cal != ea:
            return False
    elif cal is not None and cal != ea:
        # update-event and delete-event default to EA-Managed in the client
        # and refuse every other calendar there (_require_ea_managed), so
        # the flag-less form the scheduling skill documents is the hold path.
        # An explicit other calendar is refused here as before.
        return False
    # Defence in depth: an EA-Managed write never carries attendees.
    if (_flag_value(argv, "--attendees") is not None
            or _flag_value(argv, "--attendee") is not None):
        return False
    return True


def is_allowed(tool: str, verb: str, argv: list[str], *, owner: bool = False) -> bool:
    """The full un-gated allowlist: SAFE verbs plus the EA-Managed auto-hold.
    The mediator passes `owner` (is_ea_managed_autohold)."""
    return is_safe(tool, verb) or is_ea_managed_autohold(tool, verb, argv, owner=owner)


def _config_state() -> bool | None:
    """Tri-state read of ``mail_mediation.enabled``.

    True / False as configured, None when the file is missing, unreadable,
    malformed, or carries no such key. Callers must NOT collapse None to False;
    that is a fail-open, since ea-bot owns the config file.
    """
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    v = cfg.get("mail_mediation")
    if isinstance(v, dict):
        v = v.get("enabled")
    return None if v is None else bool(v)


def _resident_can_write(path: Path) -> bool:
    """True if the RESIDENT user could write `path` (unknown counts as yes).

    Deliberately not "can I write it": an owner-side caller asking whether the
    off-switch is trustworthy needs to know whether *ea-bot* could have flipped
    it, not whether the owner could. A statement the resident can rewrite is not an
    authority over the resident.
    """
    import grp
    import pwd
    try:
        st = os.stat(path)
    except OSError:
        return True
    try:
        pw = pwd.getpwnam(RESIDENT_USER)
    except Exception:
        # No resident user on this host (a dev box, a bare CI runner): no
        # resident could have written anything, so only a file anyone may
        # write counts; the caller's own files are the caller's authority.
        return bool(st.st_mode & stat.S_IWOTH)
    if st.st_uid == pw.pw_uid:
        return bool(st.st_mode & stat.S_IWUSR)
    gids = {pw.pw_gid}
    try:
        gids |= {g.gr_gid for g in grp.getgrall() if RESIDENT_USER in g.gr_mem}
    except Exception:
        pass
    if st.st_gid in gids:
        return bool(st.st_mode & stat.S_IWGRP)
    return bool(st.st_mode & stat.S_IWOTH)


def _authoritative(path: Path) -> bool:
    """True when `path` states something the resident could not have written."""
    try:
        if not path.exists():
            return False
    except OSError:
        return False
    return not (_resident_can_write(path) or _resident_can_write(path.parent))


def _trusted_off_switch() -> bool:
    """True only when an authority the resident CANNOT forge says 'off'."""
    kill = EA_DIR / "auth" / "mediation-disabled"
    if _authoritative(kill):
        return True
    return _config_state() is False and _authoritative(CONFIG)


def _is_resident() -> bool:
    """True when this process runs as the prompt-injectable resident user."""
    try:
        import pwd
        return os.geteuid() == pwd.getpwnam(RESIDENT_USER).pw_uid
    except Exception:
        return False


def mediation_active_for_resident() -> bool:
    """Is the resident mediated on this host? Answerable from EITHER side.

    Fail CLOSED: an unreadable, malformed or absent config does not disable the
    gate. Owner-side callers that read the ea-bot-owned config directly and
    honour ``enabled: false`` from it can be talked into handing the resident
    GRAPH_CLIENT_SECRET; they should call this instead.
    """
    if _trusted_off_switch():
        return False
    if MEDIATOR_UNIT.exists():
        return True             # installed -> mediate, whatever the config says
    return _config_state() is True


def should_mediate() -> bool:
    """True only for the ea-bot user, unless owner/root explicitly says otherwise.

    See the module docstring for the full decision order.
    """
    if os.environ.get("EA_FORCE_MEDIATE") == "1":
        return True
    if not _is_resident():
        return False            # the owner side executes; it never forwards
    return mediation_active_for_resident()


def _within_allowed(path_str: str) -> bool:
    """True if path resolves inside an allowed root and no component is a
    symlink escaping it (defends against symlink redirection)."""
    roots = [r.resolve() for r in _allowed_roots()]
    try:
        p = Path(path_str)
        rp = p.resolve()
    except Exception:
        return False
    inside = any(rp == r or str(rp).startswith(str(r) + os.sep) for r in roots)
    if not inside:
        return False
    # Reject if the literal target (or an existing ancestor) is a symlink whose
    # real location left the allowed roots -- resolve() already collapsed it, so
    # a mismatch between the lexical and resolved path under a root is the tell.
    try:
        if p.is_symlink() and not any(
                Path(os.path.realpath(p)) == r
                or str(Path(os.path.realpath(p))).startswith(str(r) + os.sep)
                for r in roots):
            return False
    except OSError:
        return False
    return True


def file_arg_positions(argv: list[str]):
    """Yield (index, flag, value, attached) for every FILE_FLAGS use in argv.

    `index` is the argv slot holding the VALUE, and `attached` says which
    spelling was used, so a caller can rewrite the value in place (the mediator
    does, to redirect the child at a private staging path).

    Handles BOTH argv spellings argparse accepts: ``--body-file /path`` AND
    ``--body-file=/path``. Matching the bare token only would let the ``=``
    form skip path confinement on both sides of the boundary, since the owner-side
    mediator re-runs this same function.
    """
    for i, a in enumerate(argv):
        if a in FILE_FLAGS:
            if i + 1 < len(argv):
                yield i + 1, a, argv[i + 1], False
            continue
        name, sep, value = a.partition("=")
        if sep and name in FILE_FLAGS:
            yield i, name, value, True


def _file_args(argv: list[str]):
    """Yield (flag, value) for every FILE_FLAGS occurrence in argv."""
    for _, flag, value, _attached in file_arg_positions(argv):
        yield flag, value


# Options that name a mail object by id. Graph and Gmail ids use only these
# characters; anything else (a slash, a dot, a percent escape) could name the
# same object through another URL spelling and so escape its draft lock.
ID_FLAGS = {"--message-id", "--draft-id", "--conversation-id", "--event-id",
            "--thread-id", "--reply-to-message-id", "--attachment-id"}
_ID_RE = re.compile(r"[A-Za-z0-9_=+-]{1,512}")
# Gmail attachment ids run longer than message ids; the characters are the same.
_LONG_ID_RE = re.compile(r"[A-Za-z0-9_=+-]{1,4096}")
_LONG_ID_FLAGS = {"--attachment-id"}


def is_plain_id(value, *, long: bool = False) -> bool:
    """True when `value` is a plain mail id: only the characters Graph and
    Gmail ids use, the mediator's own test for every ID_FLAGS value. An
    owner process that builds a Graph path from an id in resident space
    checks it with this first."""
    return isinstance(value, str) and bool(
        (_LONG_ID_RE if long else _ID_RE).fullmatch(value))


def _id_args(argv: list[str]):
    """Yield (flag, value) for every ID_FLAGS use, either spelling."""
    for i, a in enumerate(argv or []):
        if a in ID_FLAGS:
            if i + 1 < len(argv):
                yield a, str(argv[i + 1])
            continue
        name, sep, value = a.partition("=")
        if sep and name in ID_FLAGS:
            yield name, value


def id_error(argv: list[str], *, allow_empty: bool = False) -> str | None:
    """The refusal for the first ID_FLAGS value in argv that is not a plain
    mail id, else None. `allow_empty` lets an empty value pass (an owner
    caller's "no conversation id")."""
    for flag, value in _id_args(argv):
        if allow_empty and value == "":
            continue
        if not is_plain_id(value, long=flag in _LONG_ID_FLAGS):
            return f"{flag} ({value!r}) is not a plain mail id"
    return None


# Verbs whose --message-id names a token's subject, not a mail object.
_NOT_ID_VERBS = {("graph_actions", "request-confirm")}


def refuse_unplain_ids(tool: str, verb: str, argv: list[str]) -> None:
    """Call right after parse_args() in a mail tool. Exit 2 when an id
    option holds anything but a plain mail id (or nothing). The mediator
    checks the resident's requests; this holds an owner process that runs
    the tool directly (the bot, the remote briefing, a cron) with an id it
    read in resident space to the same test."""
    if (tool, verb) in _NOT_ID_VERBS:
        return
    err = id_error(argv, allow_empty=True)
    if err:
        _refuse(f"refused: {err}")


def sanitize(tool: str, verb: str, argv: list[str]) -> str | None:
    """Return an error string if the request is unsafe, else None.

    Enforced on BOTH sides: the ea-bot shim (fail fast) and the owner-side service
    (the real boundary, which never trusts the client). Option spellings are
    checked first, since path confinement matches file flags by exact
    spelling."""
    err = option_error(tool, verb, argv)
    if err:
        return err
    for flag, value in _file_args(argv):
        if not _within_allowed(value):
            return f"file path for {flag} ({value!r}) is outside ea-bot dirs"
    return id_error(argv)


def _refuse(msg: str, code: int = 2):
    print(json.dumps({"status": "error", "message": msg}), file=sys.stderr)
    sys.exit(code)


def forward_or_none(tool: str, argv: list[str], transform=None):
    """Call as the first line of a client's main(). Never returns when it
    mediates; returns None to let the client run directly. `transform`
    rewrites the mediator's stdout before it is printed (the id handles
    a run's reads get)."""
    if not should_mediate():
        return None
    verb = argv[0] if argv else ""
    autohold = is_ea_managed_autohold(tool, verb, argv)
    if is_gated(tool, verb) and not autohold:
        _refuse(f"BLOCKED: '{verb}' sends or mutates externally and runs only "
                f"owner-side after a Slack button. Create the draft/event and the "
                f"bot will post the confirmation.")
    if not (is_safe(tool, verb) or autohold):
        _refuse(f"unknown or unmediated verb '{tool} {verb}'")
    err = sanitize(tool, verb, argv)
    if err:
        _refuse(f"BLOCKED: {err}")

    QUEUE.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(QUEUE, 0o2770)  # group-writable so the owner-side mediator can reply
    except OSError:
        pass
    rid = f"{int(time.time() * 1000)}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    req = QUEUE / f"req-{rid}.json"
    resp = QUEUE / f"resp-{rid}.json"
    tmp = QUEUE / f".req-{rid}.tmp"
    tmp.write_text(json.dumps({"tool": tool, "verb": verb, "argv": list(argv),
                               "ts": time.time(),
                               # Dispatch identity for the draft-events journal.
                               # The mediator's child runs as the owner with a fresh
                               # env, so the run id must ride in the request or
                               # every mediated record is unlabelled.
                               "run_id": os.environ.get("EA_RUN_ID", "")}))
    try:
        os.chmod(tmp, 0o640)
    except OSError:
        pass
    os.replace(tmp, req)

    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        if resp.exists():
            try:
                r = json.loads(resp.read_text())
            except Exception:
                time.sleep(POLL_S)
                continue
            try:
                resp.unlink()
            except OSError:
                pass
            out = r.get("stdout", "")
            if transform is not None and int(r.get("rc", 0)) == 0:
                try:
                    out = transform(out)
                except Exception:
                    pass
            sys.stdout.write(out)
            sys.stderr.write(r.get("stderr", ""))
            sys.exit(int(r.get("rc", 0)))
        time.sleep(POLL_S)
    _refuse("mail mediator did not respond in time "
            "(is ea-mail-mediator.service running?)", code=3)
