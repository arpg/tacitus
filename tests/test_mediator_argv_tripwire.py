"""Tripwire: what the mediator runs as the owner is what it checked.

The mail mediator (preprocessor/mail_mediator.py) takes a request the
resident wrote, {tool, verb, argv}, checks it with mail_mediation's verb
classes and path confinement, and runs the tool with that argv as the owner.
The checks read argv by its spelling: a file argument is a token in
mail_mediation.FILE_FLAGS, as `--flag value` or `--flag=value`, and the verb
is the request's own field. The tool reads the same argv with argparse,
which also takes a unique prefix of a long option (`--body-f` for
`--body-file`) and takes its subcommand from argv itself. A test of the
checks with a list of spellings someone wrote down passes as long as the
list is the one the code expects; a spelling argparse takes and the list
leaves out runs as the owner unchecked.

So this file judges the outcome, with the tool's real parser:

  argv forms   for every mediated tool and every verb the registry lets
               through (mail_mediation.SAFE, plus the EA-Managed calendar
               verbs), and every path-valued option of that verb, the forms
               are made from the parser's own option strings: each long
               option and every prefix of it, with the value apart and
               after `=`, and each short option with the value apart,
               attached and after `=`. The value is a decoy outside every
               allowed root.
  the run      each request goes through mail_mediator.handle() with the
               child process stubbed. When the mediator refuses, the case
               holds. When it starts the child, the child's argv is parsed
               with the tool's build_parser(), as the child would parse it,
               and every path-valued option the parser resolves (other than
               its default) lies in the mediator's staging directory or in
               an allowed root. A parse error holds: the child exits before
               it opens anything.
  the verb     a request whose verb field is one the mediator allows and
               whose argv names another subcommand, or sets an option of the
               tool's top-level parser, is refused, or the parser resolves
               the allowed verb with every top-level option at its default.
  repeats      argparse keeps the last use of an option that does not
               append, and a check of argv may read another use. For every
               option of a mediated verb that the parser reads once, a
               request that sets it twice is refused (each case controlled
               by the same option set once, which runs), a repeated append
               option runs, and the options the mediator lets repeat are
               the parser's append, extend and count actions.
  coverage     every path-valued option of a mediated verb, by every one of
               its option strings, is in FILE_FLAGS, and no mediated verb
               takes a positional argument the confinement cannot see. A new
               file option cannot reach a mediated verb without confinement.

A path-valued option is one whose dest has a path word (file, path, output,
out, dest, dir, input, screenshot) or whose type is pathlib.Path or
argparse.FileType. The classifier is checked against FILE_FLAGS in turn:
every option that FILE_FLAGS names on a mediated parser is path-valued here.

The verbs come from the registry and the options from the parsers, so a new
verb or option is covered when it is added. Owner plugin verbs are left out:
their classes come from the owner's plugin lock, which is not in this tree.
Each case controls itself: the same option, spelled exactly and given a
file in an allowed root, reaches the child with its value staged, so a
harness that refuses everything cannot pass.

No child process runs and no case reads the decoy; the requests, the staging
directory and the mediator's audit log are in a sandbox under the system
temporary directory.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

SANDBOX = Path(tempfile.mkdtemp(prefix="ea-test-argv-tripwire-"))
EA_DIR = SANDBOX / "ea"
os.environ["EA_DIR"] = str(EA_DIR)
# The mediator's draft locks live in the owner-private directory.
os.environ["EA_PRIVATE_DIR"] = str(SANDBOX / "private")
# The one calendar the un-gated create/update/delete-event may name. A
# resident process reads it from the environment
# (mail_mediation.ea_managed_calendar_id); the mediator reads it from the
# owner-private calendars.json only.
EA_MANAGED = "tripwire-ea-managed@group.calendar.google.com"
os.environ["EA_MANAGED_CALENDAR_ID"] = EA_MANAGED
(SANDBOX / "private").mkdir(mode=0o700, exist_ok=True)
(SANDBOX / "private" / "calendars.json").write_text(
    json.dumps({"ea_managed_calendar_id": EA_MANAGED}))
os.chmod(SANDBOX / "private" / "calendars.json", 0o600)
for _sub in ("config", "state", "uploads", "receipts", "docs", "research", "logs", "auth"):
    (EA_DIR / _sub).mkdir(parents=True, exist_ok=True)

import mail_mediation as M  # noqa: E402
import mail_mediator as MM  # noqa: E402

# Outside every allowed root on any host: /etc is never one (the sandbox
# can be, since /tmp is). Nothing opens it.
DECOY = "/etc/ea-argv-tripwire-decoy"
IN_ROOT_INPUT = EA_DIR / "state" / "tripwire-input.txt"
IN_ROOT_OUTPUT = EA_DIR / "state" / "tripwire-output.bin"
IN_ROOT_INPUT.write_text("tripwire body\n")

# A file option whose content the owner-side mediator reads from the staging
# copy and passes to the resident's worker inline, so no path reaches the
# child (test_a_polls_slot_file_reaches_the_worker_inline).
INLINED_FILES = {("poll_filler", "available_file")}

PATH_WORDS = {"file", "files", "path", "paths", "output", "out", "dest", "dir",
              "directory", "input", "screenshot"}


# ── the registry and the parsers ──────────────────────────────────────

def _authorized(tool: str) -> list[str]:
    """Every verb of `tool` the mediator may run: SAFE, plus the calendar
    verbs the EA-Managed carve-out lets through with the right argv."""
    verbs = set(M.SAFE.get(tool, set()))
    if tool == "google_calendar_client":
        verbs |= set(M._EA_MANAGED_VERBS)
    return sorted(verbs)


_PARSERS: dict[str, argparse.ArgumentParser] = {}


def _parser(tool: str) -> argparse.ArgumentParser:
    if tool not in _PARSERS:
        _PARSERS[tool] = importlib.import_module(tool).build_parser()
    return _PARSERS[tool]


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subs) == 1, f"{parser.prog}: one subcommand set expected"
    return subs[0]


def _sub(tool: str, verb: str) -> argparse.ArgumentParser:
    return _subparsers(_parser(tool)).choices[verb]


def _own_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions
            if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))]


def _path_valued(action: argparse.Action) -> bool:
    if action.nargs == 0:
        return False
    words = set(str(action.dest).lower().replace("-", "_").split("_"))
    if words & PATH_WORDS:
        return True
    return action.type is pathlib.Path or isinstance(action.type, argparse.FileType)


def _is_output(action: argparse.Action) -> bool:
    return any(s in M.OUTPUT_FLAGS for s in action.option_strings)


MEDIATED = [(tool, verb) for tool in MM.TOOLS for verb in _authorized(tool)]


# ── argv ──────────────────────────────────────────────────────────────

def _value(action: argparse.Action) -> str:
    if action.choices:
        return str(list(action.choices)[0])
    if action.type in (int, float):
        return "1"
    return "x"


def _one(action: argparse.Action) -> list[str]:
    """`action` set once, spelled exactly, with a harmless value."""
    flag = action.option_strings[0]
    if action.nargs == 0:
        return [flag]
    if _path_valued(action):
        target = IN_ROOT_OUTPUT if _is_output(action) else IN_ROOT_INPUT
        return [flag, str(target)]
    return [flag, _value(action)]


def _fillers(tool: str, verb: str, skip: argparse.Action | None = None) -> list[str]:
    """The arguments `verb` needs to parse, and the ones the registry needs
    to let it through."""
    sp = _sub(tool, verb)
    out: list[str] = []
    for a in _own_actions(sp):
        if a is skip or not a.required:
            continue
        out += _one(a)
    for group in sp._mutually_exclusive_groups:
        members = [a for a in group._group_actions if a is not skip]
        if group.required and members and not any(a.required for a in members):
            if not any(a is skip for a in group._group_actions):
                out += _one(members[0])
    if (tool == "google_calendar_client" and verb in M._EA_MANAGED_VERBS
            and not (skip is not None and "--calendar-id" in skip.option_strings)):
        out += ["--calendar-id", EA_MANAGED]
    return out


def _forms(parser: argparse.ArgumentParser, action: argparse.Action, value: str):
    """Every way the parser's own option strings can carry `value` to
    `action`: long options and each of their prefixes, apart and after `=`;
    short options apart, attached and after `=`."""
    pc = parser.prefix_chars
    seen = set()
    for s in action.option_strings:
        long = len(s) > 2 and s[0] in pc and s[1] in pc
        for p in ([s[:n] for n in range(3, len(s) + 1)] if long else [s]):
            forms = [[p, value], [f"{p}={value}"]] + ([] if long else [[p + value]])
            for form in forms:
                if tuple(form) not in seen:
                    seen.add(tuple(form))
                    yield form


# ── the mediator, with the child stubbed ──────────────────────────────

def _mediator(monkeypatch):
    """mail_mediator bound to the sandbox, its child replaced by a recorder.
    Returns the list the recorder appends each child argv to."""
    monkeypatch.setattr(M, "EA_DIR", EA_DIR)
    # Another test module in the same run may have pointed it elsewhere.
    monkeypatch.setenv("EA_PRIVATE_DIR", str(SANDBOX / "private"))
    monkeypatch.setattr(M, "QUEUE", EA_DIR / "state" / "mail-queue")
    monkeypatch.setattr(M, "CONFIG", EA_DIR / "config" / "ea-config.json")
    monkeypatch.setattr(MM, "AUDIT", EA_DIR / "logs" / "mail-mediator.log")
    monkeypatch.setattr(MM, "STAGING_ROOT", SANDBOX / "mediator-staging")
    # One thread: the email writer's and the image reader's requests
    # otherwise run on their own.
    monkeypatch.setattr(MM, "ASYNC_TOOLS", set())
    monkeypatch.setattr(MM, "ASYNC_VERBS", set())
    monkeypatch.setattr(MM.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_dir="/home/ea-bot"))
    M.QUEUE.mkdir(parents=True, exist_ok=True)
    started: list[list[str]] = []

    def run(cmd, **kw):
        started.append(list(cmd))
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)

    monkeypatch.setattr(MM, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return started


def _submit(started: list, tool: str, verb: str, argv: list[str]) -> list[str] | None:
    """The tool argv the owner-side child gets for this request, or None
    when the mediator refuses it."""
    before = len(started)
    rid = uuid.uuid4().hex
    req = M.QUEUE / f"req-{rid}.json"
    req.write_text(json.dumps({"tool": tool, "verb": verb, "argv": argv}))
    MM.handle(str(req))
    (M.QUEUE / f"resp-{rid}.json").unlink(missing_ok=True)
    if len(started) == before:
        return None
    cmd = started[-1]
    if tool in ("web_browser", "poll_filler"):
        assert cmd[:6] == ["sudo", "-n", "-u", "ea-bot", "env", "-i"], cmd
        assert ("EA_BROWSER_DIRECT=1" if tool == "web_browser" else "EA_POLL_DIRECT=1") in cmd
        script = cmd.index(str(MM.TOOLS[tool]))
        assert cmd[script - 1] == "python3"
        return cmd[script + 1:]
    assert cmd[1] == str(MM.TOOLS[tool]), cmd
    return cmd[2:]


def _parse(tool: str, argv: list[str]) -> argparse.Namespace | None:
    """The namespace the child would act on, or None when its parser
    stops it first."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
            return _parser(tool).parse_args(argv)
    except SystemExit:
        return None


def _inside(path: str, root: Path) -> bool:
    p = os.path.realpath(path)
    r = os.path.realpath(str(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _escapes(tool: str, verb: str, ns: argparse.Namespace) -> list[str]:
    """Path-valued options of `verb` in `ns` that resolve outside the
    staging directory and every allowed root."""
    out = []
    for a in _own_actions(_sub(tool, verb)):
        if not _path_valued(a):
            continue
        got = getattr(ns, a.dest, None)
        for v in (got if isinstance(got, list) else [got]):
            if v is None or v == "" or v == a.default:
                continue
            v = getattr(v, "name", v)
            if not (_inside(str(v), MM.STAGING_ROOT) or M._within_allowed(str(v))):
                out.append(f"{a.dest}={v}")
    return out


def _chosen(tool: str, ns: argparse.Namespace) -> str | None:
    return getattr(ns, _subparsers(_parser(tool)).dest, None)


# ── the tests ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool, verb", MEDIATED, ids=[f"{t}:{v}" for t, v in MEDIATED])
def test_every_verb_the_registry_allows_has_a_subcommand(tool, verb):
    """A verb with no subparser dies in the child's argparse (and has no
    options to check here)."""
    assert verb in _subparsers(_parser(tool)).choices


@pytest.mark.parametrize("tool, verb", MEDIATED, ids=[f"{t}:{v}" for t, v in MEDIATED])
def test_a_path_the_child_resolves_stays_in_the_allowed_roots(tool, verb, monkeypatch):
    started = _mediator(monkeypatch)
    sp = _sub(tool, verb)
    escaped = []
    for action in (a for a in _own_actions(sp) if _path_valued(a) and a.option_strings
                   and (tool, a.dest) not in INLINED_FILES):
        base = [verb] + _fillers(tool, verb, skip=action)
        # The control: exact spelling, a file in an allowed root, staged.
        exact = _submit(started, tool, verb, base + _one(action))
        assert exact is not None, f"{action.option_strings[0]}: the control request was refused"
        ns = _parse(tool, exact)
        assert ns is not None, f"the child cannot parse the control argv {exact}"
        got = getattr(ns, action.dest)
        got = got[-1] if isinstance(got, list) else got
        assert _inside(str(got), MM.STAGING_ROOT), (
            f"{action.option_strings[0]}: the control value reached the child unstaged ({got})")
        for form in _forms(sp, action, DECOY):
            child = _submit(started, tool, verb, base + form)
            if child is None:
                continue
            ns = _parse(tool, child)
            if ns is None:
                continue
            bad = _escapes(tool, verb, ns)
            if bad:
                escaped.append(f"{' '.join(form)} -> child runs with {', '.join(bad)}")
    assert not escaped, (f"{tool} {verb}: the mediator accepted argv whose path the child "
                         "resolves outside the allowed roots:\n  " + "\n  ".join(escaped))


@pytest.mark.parametrize("tool", list(MM.TOOLS))
def test_the_child_runs_the_verb_the_mediator_allowed(tool, monkeypatch):
    started = _mediator(monkeypatch)
    parser = _parser(tool)
    names = list(_subparsers(parser).choices)
    top = _own_actions(parser)
    wrong = []
    for label in _authorized(tool):
        tries = [[other] + _fillers(tool, other) for other in names if other != label]
        for a in top:
            if a.option_strings:
                tries.append(_one(a) + [label] + _fillers(tool, label))
        for argv in tries:
            child = _submit(started, tool, label, argv)
            if child is None:
                continue
            ns = _parse(tool, child)
            if ns is None:
                continue
            ran = _chosen(tool, ns)
            # A top-level dest the subcommand also defines holds the
            # subcommand's value (gmail_client's --email), so only the
            # others say a top-level option was set.
            own = ({a.dest for a in _own_actions(_sub(tool, ran))}
                   if ran in _subparsers(parser).choices else set())
            changed = [a.dest for a in top if a.dest not in own
                       and getattr(ns, a.dest, a.default) != a.default]
            if ran != label or changed:
                # Listed first: a child on a verb the registry never allows.
                rank = 0 if not M.is_allowed(tool, str(ran), child) else 1
                wrong.append((rank, f"verb {label!r}, argv {argv[:2]}... -> child runs {ran!r}"
                              + (f" with top-level {changed}" if changed else "")))
    lines = [w for _, w in sorted(wrong, key=lambda w: w[0])]
    assert not lines, (f"{tool}: the mediator checked one verb and started the child on "
                       f"another ({len(lines)} case(s)):\n  " + "\n  ".join(lines[:12])
                       + ("\n  ..." if len(lines) > 12 else ""))


@pytest.mark.parametrize("tool, verb", MEDIATED, ids=[f"{t}:{v}" for t, v in MEDIATED])
def test_every_path_option_is_known_to_the_confinement(tool, verb):
    sp = _sub(tool, verb)
    unknown = []
    for a in _own_actions(sp):
        if not a.option_strings:
            unknown.append(f"positional {a.dest!r}")
        elif _path_valued(a):
            unknown += [s for s in a.option_strings if s not in M.FILE_FLAGS]
    assert not unknown, (f"{tool} {verb}: add these to mail_mediation.FILE_FLAGS (and "
                         f"OUTPUT_FLAGS for a file the tool writes): {unknown}")


def test_the_classifier_finds_every_file_flag_on_a_mediated_parser():
    """The path-word test above is the only way an option counts as a path:
    every option that FILE_FLAGS already names must pass it, or the
    classifier is too narrow to trust for the options FILE_FLAGS lacks."""
    missed = []
    for tool, verb in MEDIATED:
        for a in _own_actions(_sub(tool, verb)):
            if set(a.option_strings) & M.FILE_FLAGS and not _path_valued(a):
                missed.append(f"{tool} {verb} {a.option_strings}")
    assert not missed, f"the path classifier misses: {missed}"


# ── repeats ───────────────────────────────────────────────────────────

_REPEATING = (argparse._AppendAction, argparse._AppendConstAction,
              argparse._ExtendAction, argparse._CountAction)


def _single(tool: str, verb: str, action: argparse.Action) -> list[str]:
    """`action` set once with a value the registry lets through: the
    EA-Managed calendar for --calendar-id on the calendar verbs."""
    if (tool == "google_calendar_client" and verb in M._EA_MANAGED_VERBS
            and "--calendar-id" in action.option_strings):
        return [action.option_strings[0], EA_MANAGED]
    return _one(action)


@pytest.mark.parametrize("tool, verb", MEDIATED, ids=[f"{t}:{v}" for t, v in MEDIATED])
def test_the_scan_knows_which_options_repeat(tool, verb):
    """The options the mediator lets repeat are exactly the ones the
    parser keeps every use of; every other option keeps its last use."""
    real = {s for a in _own_actions(_sub(tool, verb)) if isinstance(a, _REPEATING)
            for s in a.option_strings}
    assert set(M.repeatable_options(tool, verb)) == real, (tool, verb)


@pytest.mark.parametrize("tool, verb", MEDIATED, ids=[f"{t}:{v}" for t, v in MEDIATED])
def test_an_option_the_child_reads_once_is_never_repeated(tool, verb, monkeypatch):
    """argparse acts on the last use of a plain option, and a check of argv
    may read the first (the EA-Managed carve-out read the first
    --calendar-id). The mediator refuses a second use of every such option,
    by every spelling pair, and runs a repeated append option. Each case
    controls itself: the option used once reaches the child."""
    started = _mediator(monkeypatch)
    ran_twice, checked = [], 0
    for action in (a for a in _own_actions(_sub(tool, verb)) if a.option_strings):
        base = [verb] + _fillers(tool, verb, skip=action)
        once = _single(tool, verb, action)
        if _submit(started, tool, verb, base + once) is None:
            continue    # refused on its own (a button token, a gated value)
        twice = base + once + once
        if len(action.option_strings) > 1:
            twice = base + once + [action.option_strings[-1]] + once[1:]
        child = _submit(started, tool, verb, twice)
        if isinstance(action, _REPEATING):
            assert child is not None, f"{tool} {verb}: the append option {once[0]} was refused"
            continue
        checked += 1
        if child is not None:
            ran_twice.append(" ".join(twice[len(base):]))
    assert not ran_twice, (f"{tool} {verb}: the mediator ran a request that sets a "
                           f"single-value option twice: {ran_twice}")
    if tool == "google_calendar_client" and verb in M._EA_MANAGED_VERBS:
        assert checked, f"{verb}: no single-value option was checked"


def test_the_carve_out_cannot_be_split_across_two_calendar_ids(monkeypatch):
    """The finding in one case: the check reads one --calendar-id and the
    child's parser keeps the other."""
    started = _mediator(monkeypatch)
    for verb in sorted(M._EA_MANAGED_VERBS):
        base = [verb] + _fillers(tool="google_calendar_client", verb=verb,
                                 skip=next(a for a in _own_actions(
                                     _sub("google_calendar_client", verb))
                                     if "--calendar-id" in a.option_strings))
        for pair in (["--calendar-id", EA_MANAGED, "--calendar-id", "primary"],
                     ["--calendar-id", "primary", "--calendar-id", EA_MANAGED],
                     [f"--calendar-id={EA_MANAGED}", "--calendar-id=primary"]):
            assert _submit(started, "google_calendar_client", verb, base + pair) is None, (
                verb, pair)
        assert _submit(started, "google_calendar_client", verb,
                       base + ["--calendar-id", EA_MANAGED]) is not None, verb


def test_a_polls_slot_file_reaches_the_worker_inline(monkeypatch):
    started = _mediator(monkeypatch)
    ok = EA_DIR / "state" / "tripwire-slots.json"
    ok.write_text('["1795273200", "1795274100"]')
    child = _submit(started, "poll_filler", "fill-poll",
                    ["fill-poll", "--url", "https://www.when2meet.com/?1-a", "--available-file", str(ok)])
    assert child == ["fill-poll", "--url", "https://www.when2meet.com/?1-a",
                     "--available", '["1795273200", "1795274100"]'], child
    for bad in ('{"a": 1}', "not json", '[["x"]]', "[" + '"1",' * 100000 + '"2"]'):
        ok.write_text(bad)
        assert _submit(started, "poll_filler", "fill-poll",
                       ["fill-poll", "--url", "https://www.when2meet.com/?1-a", "--available-file", str(ok)]) is None, bad
    assert _submit(started, "poll_filler", "fill-poll",
                   ["fill-poll", "--url", "https://www.when2meet.com/?1-a", "--available-file", DECOY]) is None
