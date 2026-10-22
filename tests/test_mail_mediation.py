#!/usr/bin/env python3
"""Tests for the mail-mediation boundary (mail_mediation.py + mail_mediator.py).

The whole point of the subsystem is that ea-bot cannot send or read the raw
tokens, so these pin the parts that enforce it: the default-deny verb allowlist,
path confinement (no escaping ea-bot's own dirs — incl. symlink escape), gated
verbs refused on BOTH sides, and the enqueue -> execute -> reply round trip.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

# No test may touch production state: sandbox EA_DIR before the
# preprocessor import below binds module paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-mailmed-")
# The mediator's draft locks live in the owner-private directory.
os.environ["EA_PRIVATE_DIR"] = tempfile.mkdtemp(prefix="ea-test-mailmed-private-")
# The calendar the un-gated writes may touch; a resident process takes it
# from the environment (mail_mediation.ea_managed_calendar_id).
os.environ["EA_MANAGED_CALENDAR_ID"] = "uma.thornbury68@example.com"
# The mediator reads it from the owner-private calendars.json only.
_CALENDARS = Path(os.environ["EA_PRIVATE_DIR"]) / "calendars.json"
_CALENDARS.write_text(json.dumps({"ea_managed_calendar_id": "uma.thornbury68@example.com"}))
os.chmod(_CALENDARS, 0o600)

import mail_mediation as M  # noqa: E402


def _setup(td):
    tmp = Path(td)
    for d in ("config", "state", "uploads", "receipts", "docs", "research",
              "logs", "auth"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    M.EA_DIR = tmp
    M.QUEUE = tmp / "state" / "mail-queue"
    M.CONFIG = tmp / "config" / "ea-config.json"
    M.MEDIATOR_UNIT = tmp / "no-such-unit"     # "mediation not installed"
    os.environ.pop("EA_FORCE_MEDIATE", None)
    return tmp


def _mediator(tmp):
    """A mail_mediator bound to this test's EA_DIR, with crh-private staging."""
    import mail_mediator
    importlib.reload(mail_mediator)            # pick up patched M.QUEUE/EA_DIR
    mail_mediator.AUDIT = tmp / "logs" / "mail-mediator.log"
    mail_mediator.STAGING_ROOT = tmp / "mediator-staging"
    M.QUEUE.mkdir(parents=True, exist_ok=True)
    return mail_mediator


def _run_request(mm, tool, verb, argv, fake_child):
    """Drive one request through the mediator with a stubbed child process.

    `fake_child(cmd) -> (stdout, rc)` stands in for the crh-side client tool;
    whatever it does to the filesystem happens INSIDE the mediator's execution
    window, which is where the TOCTOU race lives.
    """
    rid = uuid.uuid4().hex[:8]
    (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
        {"tool": tool, "verb": verb, "argv": argv}))
    real = mm.subprocess

    def run(cmd, **kw):
        out, rc = fake_child(cmd)
        return types.SimpleNamespace(stdout=out, stderr="", returncode=rc)

    mm.subprocess = types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired)
    try:
        mm.handle(str(M.QUEUE / f"req-{rid}.json"))
    finally:
        mm.subprocess = real
    return json.loads((M.QUEUE / f"resp-{rid}.json").read_text())


def _flag_value(cmd, flag):
    for i, a in enumerate(cmd):
        if a == flag:
            return cmd[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    raise AssertionError(f"{flag} not in {cmd}")


def test_classification():
    assert M.is_safe("graph_actions", "search-mail")
    # list-mail only reads one folder newest-first -> SAFE, so the resident
    # (ea-bot, no token) can run it through the crh mediator.
    assert M.is_safe("graph_actions", "list-mail")
    assert M.is_allowed("graph_actions", "list-mail",
                        ["list-mail", "--folder", "sentitems"])
    assert not M.is_gated("graph_actions", "list-mail")
    assert M.is_safe("gmail_client", "search")
    # attachment reads the Gmail MCP cannot do; download-attachment's
    # --output rides the OUTPUT_FLAGS staging, same as the graph twin
    assert M.is_safe("gmail_client", "list-attachments")
    assert M.is_safe("gmail_client", "download-attachment")
    assert not M.is_gated("gmail_client", "download-attachment")
    # list-drafts reads ids and times only; the bot uses it to find a draft
    # the Gmail MCP created. Sending stays GATED.
    assert M.is_safe("gmail_client", "list-drafts")
    assert not M.is_gated("gmail_client", "list-drafts")
    assert M.is_safe("google_calendar_client", "get-events")
    # list-ea-managed reads the EA's own rule calendar. It is the first
    # step of the un-gated EA-Managed flow (list the holds, then move or
    # delete one), and a read of one calendar is not a write to any.
    assert M.is_safe("google_calendar_client", "list-ea-managed")
    assert not M.is_gated("google_calendar_client", "list-ea-managed")
    assert M.is_allowed("google_calendar_client", "list-ea-managed",
                        ["list-ea-managed", "--days", "60"])
    assert M.is_gated("graph_actions", "send-draft")
    assert M.is_gated("graph_actions", "create-event")
    # clear-hold releases only the EA's own tagged tentative Outlook holds
    # (graph_client.clear_hold re-checks crh-side); delete-event stays GATED
    assert M.is_safe("graph_actions", "clear-hold")
    assert not M.is_gated("graph_actions", "clear-hold")
    assert M.is_gated("graph_actions", "delete-event")
    # the EA-Managed hold path as the scheduling skill documents it: update
    # and delete default to that calendar in the client, so the flag-less
    # form is allowed; naming another calendar is not; create must name it
    ea = M.ea_managed_calendar_id()
    assert M.is_allowed("google_calendar_client", "delete-event",
                        ["delete-event", "--event-id", "abc"])
    assert M.is_allowed("google_calendar_client", "update-event",
                        ["update-event", "--event-id", "abc", "--start", "2026-10-01T10:00:00"])
    assert M.is_allowed("google_calendar_client", "delete-event",
                        ["delete-event", "--event-id", "abc", "--calendar-id", ea])
    assert not M.is_allowed("google_calendar_client", "delete-event",
                            ["delete-event", "--event-id", "abc", "--calendar-id", "primary"])
    assert not M.is_allowed("google_calendar_client", "create-event",
                            ["create-event", "--email", "u@x", "--summary", "HOLD", "--start", "a", "--end", "b"])
    assert M.is_allowed("google_calendar_client", "create-event",
                        ["create-event", "--email", "u@x", "--summary", "HOLD", "--start", "a", "--end", "b",
                         "--calendar-id", ea])
    assert M.is_gated("gmail_client", "send-draft")
    assert M.is_gated("google_calendar_client", "create-event")
    # guessed verbs are neither safe nor gated
    assert not M.is_safe("graph_actions", "list-drafts")
    assert not M.is_gated("graph_actions", "list-drafts")
    # interactive setup is not mediated
    assert not M.is_safe("gmail_client", "setup")
    # the scheduling solver is read-only -> SAFE (ea-bot has no Outlook token,
    # so it MUST forward to the crh mediator to run at all)
    assert M.is_safe("scheduling_engine", "propose-meeting")
    assert M.is_safe("scheduling_engine", "validate-draft")
    assert M.is_safe("scheduling_engine", "reschedule-brief")   # reads only
    assert M.is_safe("scheduling_engine", "check-attendees")    # getSchedule read
    assert M.is_allowed("scheduling_engine", "propose-meeting", [])
    assert not M.is_gated("scheduling_engine", "propose-meeting")
    assert not M.is_safe("scheduling_engine", "bogus")


def test_should_mediate_is_resident_only():
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        # euid is not ea-bot in the test runner -> the crh side never forwards,
        # whatever the config says (it is the side that executes).
        assert M.should_mediate() is False
        M.CONFIG.write_text(json.dumps({"mail_mediation": {"enabled": True}}))
        assert M.should_mediate() is False
        os.environ["EA_FORCE_MEDIATE"] = "1"
        try:
            assert M.should_mediate() is True
        finally:
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_unreadable_config_fails_closed_on_a_mediated_host():
    """A broken config must MEDIATE, not run direct. Returning False on
    any exception would let ea-bot turn its own gate off by writing one
    brace into the config file it owns."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        unit = tmp / "ea-mail-mediator.service"     # root-owned in production
        unit.write_text("[Service]\n")
        M.MEDIATOR_UNIT = unit
        for content in ("{", "", "not json at all", "[]"):
            M.CONFIG.write_text(content)
            assert M.mediation_active_for_resident() is True, content
        M.CONFIG.unlink()                            # deleted outright
        assert M.mediation_active_for_resident() is True
        # ...and with no mediator installed, a host that never enabled it still
        # runs direct (nothing to forward to; ea-bot holds the tokens anyway).
        M.MEDIATOR_UNIT = tmp / "no-such-unit"
        assert M.mediation_active_for_resident() is False


def test_resident_writable_off_switch_is_ignored():
    """The config is ea-bot:ea by design (enable-mail-mediation.sh writes it AS
    ea-bot), so `enabled: false` in it is not an authority over ea-bot."""
    if os.geteuid() == 0:
        return  # root can write anything; the mode test says nothing
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        unit = tmp / "ea-mail-mediator.service"
        unit.write_text("[Service]\n")
        M.MEDIATOR_UNIT = unit
        M.CONFIG.write_text(json.dumps({"mail_mediation": {"enabled": False}}))
        os.chmod(M.CONFIG, 0o666)                    # world-writable -> forgeable
        assert M.mediation_active_for_resident() is True
        # a kill file anyone may write is not an off-switch either
        kill = tmp / "auth" / "mediation-disabled"
        kill.write_text("")
        os.chmod(kill, 0o666)
        assert M.mediation_active_for_resident() is True


def test_owner_owned_off_switch_is_honoured():
    """crh (or root) can still turn mediation off: an off-switch the resident
    cannot write is honoured. This is the rollback / ops lever."""
    if os.geteuid() == 0:
        return
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        unit = tmp / "ea-mail-mediator.service"
        unit.write_text("[Service]\n")
        M.MEDIATOR_UNIT = unit
        M.CONFIG.write_text(json.dumps({"mail_mediation": {"enabled": False}}))
        os.chmod(M.CONFIG, 0o644)          # owned by this user, not ea-bot
        assert M.mediation_active_for_resident() is False
        # the kill file works the same way, and outranks an enabled config
        M.CONFIG.write_text(json.dumps({"mail_mediation": {"enabled": True}}))
        assert M.mediation_active_for_resident() is True
        kill = tmp / "auth" / "mediation-disabled"
        kill.write_text("")
        os.chmod(kill, 0o644)
        assert M.mediation_active_for_resident() is False


def test_path_confinement():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        ok = str(tmp / "receipts" / "a.pdf")
        assert M.sanitize("graph_actions", "download-attachment",
                          ["download-attachment", "--output", ok]) is None
        assert M.sanitize("graph_actions", "download-attachment",
                          ["download-attachment", "--output", "/tmp/x.png"]) is None
        assert M.sanitize("graph_actions", "search-mail",
                          ["search-mail", "--query", "hi"]) is None
        # escapes rejected
        for argv in (
            ["download-attachment", "--output", "/home/crh/.ssh/authorized_keys"],
            ["create-reply", "--body-file", "/etc/passwd"],
            ["attach-file", "--path", "/home/crh/secret.txt"],
            ["download-attachment", "--output",
             str(tmp / "state") + "/" + "../" * 8 + "etc/shadow"],  # ..-escape to /etc
        ):
            assert M.sanitize("graph_actions", argv[0], argv) is not None, argv


def test_docs_is_an_allowed_root_and_its_siblings_are_not():
    """~/.ea/docs is where the EA keeps the documents it builds per
    project, so download-attachment writes there directly rather than
    downloading to state and moving. It is the same trust class as the
    other allowed roots: ea-bot owns it. The crh-owned auth/ and config/
    stay out, which is what the confinement is for."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        for ok in (str(tmp / "docs" / "sponsor" / "budget.pdf"),
                   str(tmp / "docs" / "a.pdf"),
                   f"--output={tmp / 'docs' / 'a.pdf'}"):
            argv = (["download-attachment", ok] if ok.startswith("--output=")
                    else ["download-attachment", "--output", ok])
            assert M.sanitize("graph_actions", "download-attachment",
                              argv) is None, argv
        # The sandbox EA_DIR lives under /tmp, which is itself an allowed
        # root, so a sibling path there proves nothing. Check the root list
        # instead: docs is in, and the two crh-owned directories are not.
        names = {r.name for r in M._allowed_roots()}
        assert "docs" in names
        assert "research" in names  # the owner/EA shared research folder
        assert "exports" in names and "presentations" in names, "the EA's exports and the decks it builds"
        assert not ({"auth", "config"} & names)
        roots = M._allowed_roots()
        assert any(r == M.EA_DIR.parent / "presentations" for r in roots), "the deck workspace beside ~/.ea"
        for bad in ("/home/ea-bot/.ea/auth/graph-token.json",
                    "/home/ea-bot/.ea/config/ea-config.json"):
            assert M.sanitize("graph_actions", "download-attachment",
                              ["download-attachment", "--output", bad]) is not None, bad


def test_path_confinement_equals_form():
    """argparse accepts --flag=value as well as --flag value. Checking
    only the second spelling lets --body-file=/home/crh/.ssh/id_rsa reach
    the crh mediator with no path check at all, so both spellings must
    behave identically, for READ and WRITE flags."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        # allowed paths still pass in both forms
        ok = str(tmp / "receipts" / "a.pdf")
        for argv in (["download-attachment", "--output", ok],
                     [f"download-attachment", f"--output={ok}"],
                     ["download-attachment", "--output=/tmp/x.png"],
                     ["search-mail", "--query", "a=b"],          # not a file flag
                     ["search-mail", "--query=--output=/etc/x"]):  # ditto
            assert M.sanitize("graph_actions", argv[0], argv) is None, argv
        # escapes rejected in BOTH forms
        for space, equals in (
            (["create-draft", "--body-file", "/home/crh/.ssh/id_rsa"],
             ["create-draft", "--body-file=/home/crh/.ssh/id_rsa"]),
            (["download-attachment", "--output", "/home/crh/.bashrc"],
             ["download-attachment", "--output=/home/crh/.bashrc"]),
            (["attach-file", "--file", "/home/crh/.ea/.env"],
             ["attach-file", "--file=/home/crh/.ea/.env"]),
            (["attach-file", "--path", "/etc/shadow"],
             ["attach-file", "--path=/etc/shadow"]),
        ):
            assert M.sanitize("graph_actions", space[0], space) is not None, space
            assert M.sanitize("graph_actions", equals[0], equals) is not None, equals
        # a flag with no value at all is not a way through either
        assert M.sanitize("graph_actions", "create-draft",
                          ["create-draft", "--body-file="]) is not None


def test_mediator_rejects_equals_form_server_side():
    """The mediator re-validates with the same sanitize(), so the = form must
    be refused crh-side too -- that is the half that executes as crh."""
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        import mail_mediator
        importlib.reload(mail_mediator)
        mail_mediator.AUDIT = M.EA_DIR / "logs" / "mail-mediator.log"
        M.QUEUE.mkdir(parents=True, exist_ok=True)
        for rid, argv in (
            ("eqread", ["create-draft", "--to", "rio.sorensen85@example.com", "--subject", "x",
                        "--body-file=/home/crh/.ssh/id_rsa"]),
            ("eqwrite", ["download-attachment", "--message-id", "m",
                         "--attachment-id", "a", "--output=/home/crh/.bashrc"]),
        ):
            (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
                {"tool": "graph_actions", "verb": argv[0], "argv": argv}))
            mail_mediator.handle(str(M.QUEUE / f"req-{rid}.json"))
            resp = json.loads((M.QUEUE / f"resp-{rid}.json").read_text())
            assert resp["rc"] == 2 and "refused" in resp["stderr"], (rid, resp)
            assert not (M.QUEUE / f"req-{rid}.json").exists()


def test_forward_refuses_equals_form_shim_side():
    """The ea-bot shim must exit non-zero instead of enqueueing the = form."""
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        try:
            for argv in (["create-draft", "--body-file=/home/crh/.ssh/id_rsa"],
                         ["download-attachment", "--output=/home/crh/.bashrc"]):
                try:
                    M.forward_or_none("graph_actions", argv)
                    assert False, f"should have refused {argv}"
                except SystemExit as e:
                    assert e.code and e.code != 0
            assert not list(M.QUEUE.glob("req-*.json"))  # nothing enqueued
        finally:
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_symlink_escape_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        link = tmp / "receipts" / "sneaky"
        try:
            os.symlink("/etc/hostname", link)
        except OSError:
            return  # environment can't symlink; skip
        assert M.sanitize("graph_actions", "download-attachment",
                          ["download-attachment", "--output", str(link)]) is not None


# ---------------------------------------------------------------------------
# TOCTOU on mediated paths. Confinement is checked in the shim and again
# in the mediator, but the crh-side write lands seconds later, after two Graph
# round trips, and both allowed roots are ea-bot-writable. These tests actually
# perform the swap inside the execution window.
# ---------------------------------------------------------------------------

def test_output_is_staged_and_delivered():
    """The child must be handed a path the resident does not control, and the
    bytes must still arrive where the resident asked."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        dest = str(tmp / "receipts" / "trip" / "hotel.pdf")   # dir not created
        seen = {}

        def child(cmd):
            out = _flag_value(cmd, "--output")
            seen["out"] = out
            Path(out).write_bytes(b"%PDF-1.4 receipt")
            return json.dumps({"saved": out, "size": 16}), 0

        resp = _run_request(mm, "graph_actions", "download-attachment",
                            ["download-attachment", "--message-id", "m",
                             "--attachment-id", "a", "--output", dest], child)
        assert resp["rc"] == 0, resp
        assert seen["out"] != dest                      # never the caller's path
        assert str(mm.STAGING_ROOT) in seen["out"]
        assert Path(dest).read_bytes() == b"%PDF-1.4 receipt"
        # the caller is told its own path, not the staging one
        assert json.loads(resp["stdout"])["saved"] == dest
        assert not list(mm.STAGING_ROOT.iterdir())      # staging reaped
        assert not Path(seen["out"]).exists()


def test_symlink_planted_during_execution_is_not_followed():
    """The TOCTOU scenario: /tmp/ea-recv-1 does not exist at check time, so both
    checks pass; ea-bot then points it at a crh file while the child is talking
    to Graph. The delivery must refuse instead of writing through the link."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        victim = tmp / "receipts" / "authorized_keys"     # stands in for ~crh/.ssh
        victim.write_text("ORIGINAL")
        dest = str(tmp / "state" / "ea-recv-1")

        def child(cmd):
            out = _flag_value(cmd, "--output")
            Path(out).write_bytes(b"ssh-rsa ATTACKER")
            os.symlink(victim, dest)          # <-- the swap, mid-flight
            return json.dumps({"saved": out}), 0

        resp = _run_request(mm, "graph_actions", "download-attachment",
                            ["download-attachment", "--message-id", "m",
                             "--attachment-id", "a", "--output", dest], child)
        assert victim.read_text() == "ORIGINAL"          # not overwritten
        assert resp["rc"] == 2 and "refused delivery" in resp["stderr"], resp
        assert os.path.islink(dest) and not Path(dest).read_text().startswith("ssh-rsa")


def test_symlink_pointing_outside_the_roots_is_refused_at_delivery_time():
    """Delivery re-validates the destination NOW, not at enqueue: a link planted
    after the check that leaves the allowed roots is caught by that re-check."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        real_roots = M._allowed_roots
        outside = tmp / "outside"
        outside.mkdir()
        victim = outside / "bashrc"
        victim.write_text("ORIGINAL")
        # /tmp is an allowed root in production and the whole tmpdir lives
        # under it, so narrow the roots to make "outside" mean something.
        M._allowed_roots = lambda: [tmp / "uploads", tmp / "receipts",
                                    tmp / "state"]
        dest = str(tmp / "state" / "ea-recv-2")
        try:
            def child(cmd):
                Path(_flag_value(cmd, "--output")).write_bytes(b"ATTACKER")
                os.symlink(victim, dest)
                return "{}", 0

            resp = _run_request(mm, "graph_actions", "download-attachment",
                                ["download-attachment", "--message-id", "m",
                                 "--attachment-id", "a", "--output", dest],
                                child)
        finally:
            M._allowed_roots = real_roots
        assert victim.read_text() == "ORIGINAL"
        assert resp["rc"] == 2 and "refused delivery" in resp["stderr"], resp


def test_existing_plain_file_is_replaced_not_refused():
    """download-attachment has always overwritten; only links and multiply
    linked files are refused, so re-downloading still works."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        dest = tmp / "receipts" / "again.pdf"
        dest.write_bytes(b"OLD-AND-LONGER")

        def child(cmd):
            Path(_flag_value(cmd, "--output")).write_bytes(b"NEW")
            return "{}", 0

        resp = _run_request(mm, "graph_actions", "download-attachment",
                            ["download-attachment", "--message-id", "m",
                             "--attachment-id", "a", "--output", str(dest)],
                            child)
        assert resp["rc"] == 0, resp
        assert dest.read_bytes() == b"NEW"      # truncated, not appended


def test_hard_linked_destination_is_refused():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        victim = tmp / "receipts" / "victim"
        victim.write_text("ORIGINAL")
        dest = tmp / "state" / "hardlink"
        try:
            os.link(victim, dest)
        except OSError:
            return                              # filesystem cannot hard-link
        def child(cmd):
            Path(_flag_value(cmd, "--output")).write_bytes(b"ATTACKER")
            return "{}", 0

        resp = _run_request(mm, "graph_actions", "download-attachment",
                            ["download-attachment", "--message-id", "m",
                             "--attachment-id", "a", "--output", str(dest)],
                            child)
        assert victim.read_text() == "ORIGINAL"
        assert resp["rc"] == 2 and "refused delivery" in resp["stderr"], resp


def test_input_file_is_copied_into_staging_before_the_child_runs():
    """The read half of the same race: --body-file is copied in up front, so a
    swap after the check reaches a file the child no longer opens."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        body = tmp / "state" / "reply-body.txt"
        body.write_text("Hi Kit,\n")
        seen = {}

        def child(cmd):
            p = _flag_value(cmd, "--body-file")
            seen["path"] = p
            seen["text"] = Path(p).read_text()
            seen["name"] = os.path.basename(p)
            return json.dumps({"draft_id": "AAMk", "body_from": p}), 0

        resp = _run_request(mm, "graph_actions", "create-reply",
                            ["create-reply", "--message-id", "m", "--body", "x",
                             "--body-file", str(body)], child)
        assert resp["rc"] == 0, resp
        assert seen["path"] != str(body)
        assert str(mm.STAGING_ROOT) in seen["path"]
        assert seen["text"] == "Hi Kit,\n"
        assert seen["name"] == "reply-body.txt"      # attach-file names by it
        assert json.loads(resp["stdout"])["body_from"] == str(body)


def test_input_symlink_is_refused_rather_than_read_through():
    """A --body-file that IS a symlink is refused by the no-follow open, even
    when its target passes the resolve()-based confinement check."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        target = tmp / "state" / "real.txt"
        target.write_text("secret-ish")
        link = tmp / "state" / "link.txt"
        try:
            os.symlink(target, link)
        except OSError:
            return
        assert M.sanitize("graph_actions", "create-reply",
                          ["create-reply", "--body-file", str(link)]) is None
        resp = _run_request(mm, "graph_actions", "create-reply",
                            ["create-reply", "--message-id", "m", "--body", "x",
                             "--body-file", str(link)],
                            lambda cmd: ("{}", 0))
        assert resp["rc"] == 2 and "refused" in resp["stderr"], resp


def test_staging_root_does_not_come_from_the_environment():
    """The mediator unit reads EnvironmentFile=$EA_DIR/.env, which is
    ea-bot-writable, so $HOME in this process is attacker-influenced. The
    staging root comes from the passwd entry instead."""
    import mail_mediator
    old = os.environ.get("HOME")
    os.environ["HOME"] = "/tmp/attacker-home"
    try:
        root = mail_mediator._default_staging_root()
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old
    assert "attacker-home" not in str(root), root


def test_forward_refuses_gated_unknown_and_badpath():
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        try:
            for argv in (["send-draft", "--message-id", "x"],   # gated
                         ["list-drafts"],                        # unknown
                         ["download-attachment", "--output", "/etc/x"]):  # bad path
                try:
                    M.forward_or_none("graph_actions", argv)
                    assert False, f"should have refused {argv}"
                except SystemExit as e:
                    assert e.code and e.code != 0
        finally:
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_forward_round_trip():
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        M.QUEUE.mkdir(parents=True, exist_ok=True)

        def responder():
            for _ in range(400):
                reqs = list(M.QUEUE.glob("req-*.json"))
                if reqs:
                    rid = reqs[0].name[len("req-"):-len(".json")]
                    reqs[0].unlink()
                    (M.QUEUE / f"resp-{rid}.json").write_text(
                        json.dumps({"stdout": "[]", "stderr": "", "rc": 0}))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=responder)
        t.start()
        try:
            M.forward_or_none("graph_actions", ["search-mail", "--query", "hi"])
            assert False, "should have exited after the mediator replied"
        except SystemExit as e:
            assert e.code == 0
        finally:
            t.join(timeout=5)
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_list_mail_forwards_and_mediator_accepts():
    """list-mail must survive BOTH halves of the boundary: the ea-bot shim
    enqueues it (not refused as unknown/gated) and the mediator executes it."""
    argv = ["list-mail", "--folder", "sentitems", "--top", "10"]
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        assert M.sanitize("graph_actions", "list-mail", argv) is None
        os.environ["EA_FORCE_MEDIATE"] = "1"
        M.QUEUE.mkdir(parents=True, exist_ok=True)
        holder = {}

        def responder():
            for _ in range(400):
                reqs = list(M.QUEUE.glob("req-*.json"))
                if reqs:
                    holder["req"] = json.loads(reqs[0].read_text())
                    rid = reqs[0].name[len("req-"):-len(".json")]
                    reqs[0].unlink()
                    (M.QUEUE / f"resp-{rid}.json").write_text(
                        json.dumps({"stdout": "[]", "stderr": "", "rc": 0}))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=responder); t.start()
        try:
            M.forward_or_none("graph_actions", argv)
            assert False, "list-mail should forward then exit 0"
        except SystemExit as e:
            assert e.code == 0
        finally:
            t.join(timeout=5)
            os.environ.pop("EA_FORCE_MEDIATE", None)
        assert holder.get("req", {}).get("verb") == "list-mail"
        assert holder.get("req", {}).get("argv") == argv

        # server side: the mediator revalidates and must NOT refuse it
        import mail_mediator
        importlib.reload(mail_mediator)
        assert mail_mediator.M.is_allowed("graph_actions", "list-mail", argv)


def test_mediator_revalidates_server_side():
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        import mail_mediator
        importlib.reload(mail_mediator)   # pick up patched M.QUEUE/EA_DIR
        mail_mediator.AUDIT = M.EA_DIR / "logs" / "mail-mediator.log"
        M.QUEUE.mkdir(parents=True, exist_ok=True)

        # a request for a GATED verb must be refused even though it reached the service
        rid = "gatedtest"
        (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
            {"tool": "graph_actions", "verb": "send-draft",
             "argv": ["send-draft", "--message-id", "x"]}))
        mail_mediator.handle(str(M.QUEUE / f"req-{rid}.json"))
        resp = json.loads((M.QUEUE / f"resp-{rid}.json").read_text())
        assert resp["rc"] == 2 and "refused" in resp["stderr"]
        assert not (M.QUEUE / f"req-{rid}.json").exists()  # cleaned up

        # a path-escaping SAFE verb must also be refused
        rid2 = "pathtest"
        (M.QUEUE / f"req-{rid2}.json").write_text(json.dumps(
            {"tool": "graph_actions", "verb": "download-attachment",
             "argv": ["download-attachment", "--output", "/home/crh/x"]}))
        mail_mediator.handle(str(M.QUEUE / f"req-{rid2}.json"))
        resp2 = json.loads((M.QUEUE / f"resp-{rid2}.json").read_text())
        assert resp2["rc"] == 2 and "refused" in resp2["stderr"]


def test_ea_managed_autohold_exception():
    CAL = M.ea_managed_calendar_id()
    ce = "create-event"
    # EXACTLY the EA-Managed calendar, no attendees -> un-gated auto-hold
    ok = [ce, "--email", "jo.ambrose4@example.com", "--summary", "Focus",
          "--start", "2026-07-20T09:00:00", "--end", "2026-07-20T10:00:00",
          "--calendar-id", CAL]
    assert M.is_ea_managed_autohold("google_calendar_client", ce, ok)
    assert M.is_allowed("google_calendar_client", ce, ok)
    # Everything below must NOT be an auto-hold (stays button-gated):
    #   primary calendar (no --calendar-id)
    assert not M.is_ea_managed_autohold("google_calendar_client", ce, ok[:-2])
    #   a different / look-alike calendar id (exact match only)
    other = ok[:-1] + ["primary"]
    assert not M.is_ea_managed_autohold("google_calendar_client", ce, other)
    sub = ok[:-1] + ["x" + CAL]
    assert not M.is_ea_managed_autohold("google_calendar_client", ce, sub)
    #   EA-Managed but WITH attendees
    withatt = ok + ["--attendees", "rio.sorensen85@example.com"]
    assert not M.is_ea_managed_autohold("google_calendar_client", ce, withatt)
    assert not M.is_allowed("google_calendar_client", ce, withatt)
    #   the exception is calendar-create only, not send/graph create-event
    assert not M.is_ea_managed_autohold(
        "graph_actions", "create-event", ["create-event", "--calendar-id", CAL])
    #   forward_or_none forwards the auto-hold (does not refuse it)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        M.QUEUE.mkdir(parents=True, exist_ok=True)
        import threading
        holder = {}

        def responder():
            for _ in range(400):
                reqs = list(M.QUEUE.glob("req-*.json"))
                if reqs:
                    holder["argv"] = json.loads(reqs[0].read_text())["argv"]
                    rid = reqs[0].name[len("req-"):-len(".json")]
                    reqs[0].unlink()
                    (M.QUEUE / f"resp-{rid}.json").write_text(
                        json.dumps({"stdout": "{}", "stderr": "", "rc": 0}))
                    return
                time.sleep(0.02)
        t = threading.Thread(target=responder); t.start()
        try:
            M.forward_or_none("google_calendar_client", ok)
            assert False, "auto-hold should forward then exit 0"
        except SystemExit as e:
            assert e.code == 0
        finally:
            t.join(timeout=5)
            os.environ.pop("EA_FORCE_MEDIATE", None)
        assert holder.get("argv") == ok  # it really enqueued the auto-hold
        # ...but a primary-calendar create-event is refused (gated)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        try:
            M.forward_or_none("google_calendar_client", ok[:-2])
            assert False, "primary create-event must be gated"
        except SystemExit as e:
            assert e.code and e.code != 0
        finally:
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_scheduling_engine_forwards_and_mediator_accepts():
    """The solver is SAFE: forward_or_none enqueues it (ea-bot side) and the
    mediator, which now lists scheduling_engine in TOOLS, accepts it (does not
    refuse it as an unknown tool)."""
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        # ea-bot side: propose-meeting forwards (enqueue -> reply -> exit 0)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        M.QUEUE.mkdir(parents=True, exist_ok=True)
        holder = {}

        def responder():
            for _ in range(400):
                reqs = list(M.QUEUE.glob("req-*.json"))
                if reqs:
                    holder["req"] = json.loads(reqs[0].read_text())
                    rid = reqs[0].name[len("req-"):-len(".json")]
                    reqs[0].unlink()
                    (M.QUEUE / f"resp-{rid}.json").write_text(
                        json.dumps({"stdout": "{}", "stderr": "", "rc": 0}))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=responder); t.start()
        try:
            M.forward_or_none("scheduling_engine",
                              ["propose-meeting", "--duration", "30"])
            assert False, "propose-meeting should forward then exit 0"
        except SystemExit as e:
            assert e.code == 0
        finally:
            t.join(timeout=5)
            os.environ.pop("EA_FORCE_MEDIATE", None)
        assert holder.get("req", {}).get("tool") == "scheduling_engine"

        # server side: the mediator knows the tool and does NOT refuse the verb
        import mail_mediator
        importlib.reload(mail_mediator)
        assert "scheduling_engine" in mail_mediator.TOOLS
        mail_mediator.AUDIT = M.EA_DIR / "logs" / "mail-mediator.log"
        rid = "schedtest"
        (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
            {"tool": "scheduling_engine", "verb": "propose-meeting",
             "argv": ["propose-meeting", "--duration", "30"]}))
        # It will actually try to run the solver as this test user; we only care
        # that it was NOT refused as unknown-tool/unknown-verb (rc 2 + "refused").
        mail_mediator.handle(str(M.QUEUE / f"req-{rid}.json"))
        resp = json.loads((M.QUEUE / f"resp-{rid}.json").read_text())
        assert not (resp["rc"] == 2 and "mediator refused" in resp["stderr"]), \
            f"mediator wrongly refused the solver: {resp}"


def test_deck_serve_is_mediated_and_has_no_funnel_verb():
    """The deck route tool forwards as a SAFE tool with exactly three verbs.
    The mediator accepts them and refuses anything else under that tool
    name, so a raw queue request for a funnel dies at the trust boundary."""
    assert M.SAFE["deck_serve"] == {"serve", "off", "status"}
    for verb in ("funnel", "reset", "up", "set"):
        assert not M.is_allowed("deck_serve", verb, [verb])
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        assert mm.TOOLS["deck_serve"].name == "deck_serve.py"
        seen = []

        def fake_child(cmd):
            seen.append(cmd)
            return '{"status": "ok"}', 0

        resp = _run_request(mm, "deck_serve", "status", ["status"], fake_child)
        assert resp["rc"] == 0 and seen and seen[0][1].endswith("deck_serve.py"), resp
        resp = _run_request(mm, "deck_serve", "serve",
                            ["serve", "--slug", "x", "--port", "3050"], fake_child)
        assert resp["rc"] == 0 and seen[-1][-4:] == ["--slug", "x", "--port", "3050"]
        before = len(seen)
        resp = _run_request(mm, "deck_serve", "funnel", ["funnel", "--slug", "x"], fake_child)
        assert resp["rc"] == 2 and "mediator refused" in resp["stderr"]
        assert len(seen) == before, "a refused verb must never reach the child"


def test_teams_dm_read_is_a_plugin_verb_not_a_core_one():
    """The teams-dms owner plugin declares get-teams-dms as an alias on
    graph_actions. A core verb wins over an alias, so the core must not
    classify it."""
    assert "get-teams-dms" not in M.SAFE["graph_actions"]
    assert "get-teams-dms" not in M.GATED["graph_actions"]



def test_email_writer_runs_on_its_own_thread():
    """The remote email writer is SAFE with one verb; its brief is staged in
    by copy and its body delivered out; and its call runs on a thread of
    its own, so a slow writer never holds another verb in the queue."""
    assert M.SAFE["email_writer"] == {"write"}
    assert "--brief-file" in M.FILE_FLAGS and "--brief-file" not in M.OUTPUT_FLAGS
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        assert mm.TOOLS["email_writer"].name == "email_writer.py"
        brief = tmp / "state" / "brief.txt"
        brief.write_text("say yes")
        body = tmp / "state" / "body.txt"
        release = threading.Event()

        def run(cmd, **kw):
            if cmd[1].endswith("email_writer.py"):
                assert Path(_flag_value(cmd, "--brief-file")).read_text() == "say yes"
                assert _flag_value(cmd, "--brief-file") != str(brief)
                Path(_flag_value(cmd, "--out")).write_text("Hi Pat")
                release.wait(5)
                return types.SimpleNamespace(stdout='{"status": "ok"}', stderr="", returncode=0)
            return types.SimpleNamespace(stdout='{"status": "ok"}', stderr="", returncode=0)

        real = mm.subprocess
        mm.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
        try:
            (M.QUEUE / "req-w1.json").write_text(json.dumps(
                {"tool": "email_writer", "verb": "write",
                 "argv": ["write", "--to", "morgan.vasquez41@example.edu", "--brief-file", str(brief),
                          "--out", str(body)]}))
            mm.handle(str(M.QUEUE / "req-w1.json"))
            assert not (M.QUEUE / "req-w1.json").exists()
            assert not (M.QUEUE / "resp-w1.json").exists()           # still writing
            (M.QUEUE / "req-d1.json").write_text(json.dumps(
                {"tool": "deck_serve", "verb": "status", "argv": ["status"]}))
            mm.handle(str(M.QUEUE / "req-d1.json"))
            assert json.loads((M.QUEUE / "resp-d1.json").read_text())["rc"] == 0
            release.set()
            for _ in range(250):
                if (M.QUEUE / "resp-w1.json").exists():
                    break
                time.sleep(0.02)
            resp = json.loads((M.QUEUE / "resp-w1.json").read_text())
            assert resp["rc"] == 0 and body.read_text() == "Hi Pat"
        finally:
            release.set()
            mm.subprocess = real


def test_cron_lane_read_of_a_kept_thread_never_runs():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        import mail_privacy
        real = mail_privacy.release_precheck
        mail_privacy.release_precheck = lambda tool, verb, argv, run_id: (
            "kept" if run_id.startswith("cron-") else "")
        ran = []
        try:
            (M.QUEUE / "req-k1.json").write_text(json.dumps(
                {"tool": "graph_actions", "verb": "get-conversation", "run_id": "cron-morning-1",
                 "argv": ["get-conversation", "--conversation-id", "c1"]}))
            real_sub = mm.subprocess
            mm.subprocess = types.SimpleNamespace(
                run=lambda cmd, **kw: ran.append(cmd), TimeoutExpired=subprocess.TimeoutExpired)
            try:
                mm.handle(str(M.QUEUE / "req-k1.json"))
            finally:
                mm.subprocess = real_sub
            resp = json.loads((M.QUEUE / "resp-k1.json").read_text())
            assert resp["rc"] == 2 and "kept" in resp["stderr"] and ran == []
        finally:
            mail_privacy.release_precheck = real


# --- option spellings: abbreviations, unknown options, real parsers ---------

MEDIATED_TOOLS = sorted(set(M.SAFE) | set(M.GATED))

# Abbreviated spellings of file flags, both argv forms. argparse with
# allow_abbrev=True reads each one as the full file option, and FILE_FLAGS
# matches none of them.
ABBREVIATED = [
    ("graph_actions", ["create-draft", "--to", "a@example.com", "--subject", "x",
                       "--body-f", "/home/crh/.ssh/id_rsa"]),
    ("graph_actions", ["create-draft", "--to", "a@example.com", "--subject", "x",
                       "--body-fi=/home/crh/.ssh/id_rsa"]),
    ("graph_actions", ["download-attachment", "--message-id", "m",
                       "--attachment-id", "a", "--outp", "/home/crh/.bashrc"]),
    ("graph_actions", ["download-attachment", "--message-id", "m",
                       "--attachment-id", "a", "--outpu=/home/crh/.bashrc"]),
    ("graph_actions", ["attach-file", "--message-id", "m", "--fil", "/etc/shadow"]),
    ("gmail_client", ["attach-file", "--email", "a@example.com", "--draft-id", "d",
                      "--fi=/etc/shadow"]),
    ("gmail_client", ["download-attachment", "--email", "a@example.com",
                      "--message-id", "m", "--attachment-id", "a", "--outp=/home/crh/x"]),
    ("email_writer", ["write", "--to", "a@example.com", "--brief", "/home/crh/.ssh/id_rsa",
                      "--out", "/tmp/x"]),
    ("email_writer", ["write", "--to", "a@example.com", "--brief-file", "/tmp/b",
                      "--o=/home/crh/.bashrc"]),
]


def test_abbreviated_file_flags_are_refused_by_sanitize():
    """An abbreviated file flag is refused whatever path it carries, an
    allowed one included: the refusal is the spelling, not the path."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        for tool, argv in ABBREVIATED:
            err = M.sanitize(tool, argv[0], argv)
            assert err and "not an option" in err, (tool, argv, err)
        inside = str(tmp / "state" / "body.txt")
        for argv in (["create-draft", "--to", "a@example.com", "--subject", "x",
                      "--body-f", inside],
                     ["create-draft", "--to", "a@example.com", "--subject", "x",
                      f"--body-f={inside}"]):
            assert M.sanitize("graph_actions", argv[0], argv), argv
        # the full spellings still pass with an allowed path
        assert M.sanitize("graph_actions", "create-draft",
                          ["create-draft", "--to", "a@example.com", "--subject", "x",
                           "--body-file", inside]) is None
        assert M.sanitize("graph_actions", "create-draft",
                          ["create-draft", f"--body-file={inside}"]) is None


def test_mediator_refuses_abbreviated_file_flags_server_side():
    """The owner-side half refuses the abbreviation before staging, and the
    child never runs."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        for tool, argv in ABBREVIATED:
            ran = []
            resp = _run_request(mm, tool, argv[0], argv,
                                lambda cmd: (ran.append(cmd), ("", 0))[1])
            assert resp["rc"] == 2 and "not an option" in resp["stderr"], (argv, resp)
            assert ran == [], argv
        assert not list(M.QUEUE.glob("req-*.json"))
        assert "REFUSED-PATH" in (tmp / "logs" / "mail-mediator.log").read_text()


def test_shim_refuses_abbreviated_file_flags():
    """The resident's shim exits non-zero and enqueues nothing."""
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        os.environ["EA_FORCE_MEDIATE"] = "1"
        try:
            for tool, argv in ABBREVIATED:
                try:
                    M.forward_or_none(tool, argv)
                    assert False, f"should have refused {argv}"
                except SystemExit as e:
                    assert e.code == 2, (argv, e.code)
            assert not list(M.QUEUE.glob("req-*.json"))
        finally:
            os.environ.pop("EA_FORCE_MEDIATE", None)


def test_unknown_options_are_refused_and_values_are_not_options():
    with tempfile.TemporaryDirectory() as td:
        _setup(td)
        refused = [
            ("graph_actions", ["search-mail", "--query", "x", "--no-such-flag"]),
            ("graph_actions", ["search-mail", "--no-such-flag=1"]),
            ("graph_actions", ["search-mail", "--query", "x", "--", "--output", "/tmp/x"]),
            # a real option of another verb is not an option of this one
            ("graph_actions", ["search-mail", "--body-file", "/tmp/x"]),
            ("gmail_client", ["search", "--email", "a@example.com", "-q", "x"]),
            ("scheduling_engine", ["propose-meeting", "--dur", "30"]),
            ("deck_serve", ["status", "--s", "x"]),
            ("plugin_cards", ["consume-token", "--act", "x", "--token", "t"]),
            ("google_calendar_client", ["get-events", "--email", "a@example.com",
                                        "--start", "s", "--end", "e", "--days", "3"]),
            # a verb the tool's parser does not define
            ("graph_actions", ["no-such-verb", "--query", "x"]),
            ("no_such_tool", ["search-mail"]),
        ]
        for tool, argv in refused:
            assert M.sanitize(tool, argv[0], argv), (tool, argv)
        allowed = [
            ["search-mail", "--query", "x", "--top", "5"],
            ["search-mail", "--query=a=b"],
            ["search-mail", "--query=--output=/etc/x"],
            ["search-mail", "--query", "-- not an option"],   # a value with a space
            ["search-mail", "--query", "x", "--top", "-1"],   # a negative number
            ["search-mail", "--query", "-"],
            ["search-mail", "--help"],
        ]
        for argv in allowed:
            assert M.sanitize("graph_actions", argv[0], argv) is None, argv


def test_plugin_verbs_refuse_abbreviated_file_flags():
    """A plugin's parser is not readable owner-side, so a strict prefix of a
    file flag is what is refused there; full spellings and other options
    pass to the plugin's own checks."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        tool = M.PLUGIN_TOOL_PREFIX + "example"
        for argv in (["read", "--outp", "/home/crh/x"], ["read", "--body-f=/etc/x"],
                     ["read", "--fi", "/etc/x"]):
            assert M.option_error(tool, "read", argv), argv
        ok = str(tmp / "state" / "x")
        for argv in (["read", "--output", ok], ["read", "--chat-id", "c"],
                     ["read", f"--out={ok}"]):
            assert M.option_error(tool, "read", argv) is None, argv


def test_parser_scan_reads_the_shapes_it_must_refuse():
    """A computed flag name or parents= makes the verb unreadable (refused);
    aliases, groups, chained calls and module constants are read."""
    src = '''
import argparse
VERB = "named"
FLAG = "--from-const"
def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    sub = ap.add_subparsers(dest="v")
    p = sub.add_parser("one", aliases=["uno"], allow_abbrev=False)
    p.add_argument("--alpha", "-a")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--beta")
    sub.add_parser(VERB, allow_abbrev=False).add_argument(FLAG)
    p = sub.add_parser("computed", allow_abbrev=False)
    p.add_argument("--" + "x")
    p = sub.add_parser("inherits", parents=[ap], allow_abbrev=False)
    p = sub.add_parser("bare", add_help=False, allow_abbrev=False)
'''
    got = M._scan_parsers(src)
    assert set(got["one"]) == {"-h", "--help", "--alpha", "-a", "--beta"}
    assert got["one"]["-a"] == frozenset({"--alpha", "-a"})
    assert got["uno"] == got["one"]
    assert set(got["named"]) == {"-h", "--help", "--from-const"}
    assert got["computed"] is None and got["inherits"] is None
    assert got["bare"] == {}
    # a short alias of a file flag is refused: path confinement matches the
    # FILE_FLAGS spelling only
    opts = {"--output": frozenset({"--output", "-o"}), "-o": frozenset({"--output", "-o"})}
    real = M.parser_options
    M.parser_options = lambda tool, verb: opts
    try:
        assert M.option_error("graph_actions", "x", ["x", "-o", "/etc/x"])
        assert M.option_error("graph_actions", "x", ["x", "-o/etc/x"])
        assert M.option_error("graph_actions", "x", ["x", "--output", "/tmp/x"]) is None
    finally:
        M.parser_options = real


# Runs one mediated tool as __main__ with parse_args replaced: it prints the
# parser argparse built (every verb's options and allow_abbrev, and whether a
# one-character-short spelling of each long option resolves to an action)
# and exits before any command runs.
_PARSER_PROBE = r'''
import argparse, json, os, runpy, sys

def probe(self, *a, **k):
    out = {"allow_abbrev": self.allow_abbrev, "verbs": {}}
    for act in self._actions:
        if not isinstance(act, argparse._SubParsersAction):
            continue
        for verb, sp in act.choices.items():
            groups = {s: sorted(x.option_strings) for x in sp._actions
                      for s in x.option_strings}
            resolved = []
            for s in groups:
                short = s[:-1]
                if s.startswith("--") and len(short) > 2 and short not in groups:
                    for token in (short, short + "=v"):
                        hit = sp._parse_optional(token)
                        # Python 3.12 returns a list of matches, older ones
                        # one (action, option, value) tuple.
                        hits = hit if isinstance(hit, list) else [hit] if hit else []
                        if any(t and t[0] is not None for t in hits):
                            resolved.append(token)
            out["verbs"][verb] = {"allow_abbrev": sp.allow_abbrev,
                                  "options": groups, "resolved": resolved}
    print("PARSER-PROBE " + json.dumps(out))
    sys.exit(0)

argparse.ArgumentParser.parse_args = probe
tool = sys.argv[1]
sys.path[0] = os.path.dirname(os.path.abspath(tool))
sys.argv = [tool]
runpy.run_path(tool, run_name="__main__")
'''


def _real_parsers() -> dict:
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.py"
        probe.write_text(_PARSER_PROBE)
        ea = Path(td) / "ea"
        for d in ("config", "state", "auth"):
            (ea / d).mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k != "EA_FORCE_MEDIATE"}
        env["EA_DIR"] = str(ea)
        found = {}
        for tool in MEDIATED_TOOLS:
            src = HERE.parent / "preprocessor" / f"{tool}.py"
            p = subprocess.run([sys.executable, str(probe), str(src)], cwd=str(HERE.parent),
                               capture_output=True, text=True, timeout=120, env=env)
            line = next((ln for ln in p.stdout.splitlines()
                         if ln.startswith("PARSER-PROBE ")), None)
            assert line, f"{tool}: no parser probe output\n{p.stderr[-2000:]}"
            found[tool] = json.loads(line[len("PARSER-PROBE "):])
        return found


def test_mediated_parsers_reject_abbreviations_and_match_the_scan():
    """Every mediated parser and subparser has allow_abbrev=False, no
    shortened long option resolves to an action, and the option set the
    mediator reads from the source equals the one argparse builds, for
    every verb the mediator can be asked to run."""
    real = _real_parsers()
    for tool in MEDIATED_TOOLS:
        r = real[tool]
        assert r["allow_abbrev"] is False, tool
        verbs = M.SAFE.get(tool, set()) | M.GATED.get(tool, set())
        assert verbs <= set(r["verbs"]), (tool, verbs - set(r["verbs"]))
        for verb, info in r["verbs"].items():
            assert info["allow_abbrev"] is False, (tool, verb)
            assert info["resolved"] == [], (tool, verb, info["resolved"])
            scanned = M.parser_options(tool, verb)
            assert scanned is not None, (tool, verb)
            assert {k: sorted(v) for k, v in scanned.items()} == info["options"], (tool, verb)


def test_argv_must_start_with_the_declared_verb():
    """The allowlist checks the request's verb field and argparse takes the
    subcommand from argv, so a request whose argv names another subcommand,
    or puts a top-level option first, is refused on both sides."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        cases = [
            ("graph_actions", "search-mail", ["send-draft", "--message-id", "m"]),
            ("gmail_client", "search", ["send-draft", "--email", "a@example.com",
                                        "--draft-id", "d"]),
            ("google_calendar_client", "get-events",
             ["--setup", "get-events", "--email", "a@example.com"]),
            ("gmail_client", "search", ["--email", "a@example.com", "search"]),
            ("graph_actions", "search-mail", []),
            ("graph_actions", "", ["search-mail"]),
        ]
        for tool, verb, argv in cases:
            assert M.sanitize(tool, verb, argv), (verb, argv)
            ran = []
            resp = _run_request(mm, tool, verb, argv,
                                lambda cmd: (ran.append(cmd), ("", 0))[1])
            assert resp["rc"] == 2 and "refused" in resp["stderr"], (verb, argv, resp)
            assert ran == [], (verb, argv)


def test_child_confirms_the_verb_with_its_own_parser():
    """In a child the mediator started, the tool's real parser must have
    resolved the checked verb with every top-level option at its default;
    the variable is gone after the check, so the child's own children are
    not held to it."""
    import argparse
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--email")
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("search", allow_abbrev=False)
    p.add_argument("--email")
    sub.add_parser("send-draft", allow_abbrev=False)

    def outcome(want, argv):
        os.environ[M.MEDIATED_VERB_ENV] = want
        try:
            M.check_child_parse(ap, ap.parse_args(argv))
            return 0
        except SystemExit as e:
            return e.code
        finally:
            assert M.MEDIATED_VERB_ENV not in os.environ
    assert outcome("search", ["search", "--email", "a@example.com"]) == 0
    assert outcome("search", ["send-draft"]) == 2
    assert outcome("search", ["--setup", "search"]) == 2
    os.environ.pop(M.MEDIATED_VERB_ENV, None)
    M.check_child_parse(ap, ap.parse_args(["send-draft"]))   # not mediated: no check
    # the mediator names the verb for a core tool's child
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        envs = []
        real = mm.subprocess
        mm.subprocess = types.SimpleNamespace(
            run=lambda cmd, **kw: (envs.append(kw.get("env") or {}),
                                   types.SimpleNamespace(stdout="", stderr="", returncode=0))[1],
            TimeoutExpired=subprocess.TimeoutExpired)
        try:
            (M.QUEUE / "req-v1.json").write_text(json.dumps(
                {"tool": "graph_actions", "verb": "search-mail",
                 "argv": ["search-mail", "--query", "x"]}))
            mm.handle(str(M.QUEUE / "req-v1.json"))
        finally:
            mm.subprocess = real
        assert envs and envs[0].get(M.MEDIATED_VERB_ENV) == "search-mail"


def test_gmail_send_draft_needs_the_one_time_token():
    """gmail_client send-draft consumes the same token graph_actions
    send-gmail-draft does, so a send never rests on the mediator alone."""
    import gmail_client
    import graph_actions
    real_send, real_check = gmail_client.send_draft, graph_actions._validate_and_consume_token
    sent, seen = [], []
    gmail_client.send_draft = (
        lambda email, draft_id, fingerprint="": sent.append((draft_id, fingerprint)) or {"ok": True})
    graph_actions._validate_and_consume_token = (
        lambda token, action, ident: seen.append((token, action, ident))
        or ({"fingerprint": "fp1"} if token == "good" else None))
    real_argv = sys.argv
    try:
        for token, code in ((None, 2), ("bad", 2), ("good", None)):
            sys.argv = ["gmail_client.py", "send-draft", "--email", "a@example.com",
                        "--draft-id", "d1"] + (["--confirm-token", token] if token else [])
            try:
                gmail_client.main()
                got = None
            except SystemExit as e:
                got = e.code
            assert got == code, (token, got)
    finally:
        sys.argv = real_argv
        gmail_client.send_draft, graph_actions._validate_and_consume_token = real_send, real_check
    assert sent == [("d1", "fp1")]
    assert ("good", "send", "d1") in seen


def test_resident_clis_parse_without_abbreviations():
    """The preprocessor CLIs the bot names (its tool allowlist among them)
    build every parser with allow_abbrev=False, so a shortened flag is an
    error there too, not a silent match."""
    import ast
    import re
    bot = (HERE.parent / "ea-chat-bot.py").read_text()
    names = set(re.findall(r"preprocessor/([a-z_]+)\.py", bot)) | set(MEDIATED_TOOLS)
    missing = []
    for name in sorted(names):
        path = HERE.parent / "preprocessor" / f"{name}.py"
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname not in ("ArgumentParser", "add_parser"):
                continue
            ok = any(kw.arg == "allow_abbrev" and isinstance(kw.value, ast.Constant)
                     and kw.value.value is False for kw in node.keywords)
            if not ok:
                missing.append(f"{name}.py:{node.lineno}")
    assert not missing, missing


def test_mail_ids_must_be_plain():
    """An id is refused when it holds anything but the characters Graph and
    Gmail ids use, so one draft cannot be named two ways (a dot segment, a
    slash, a percent escape) and take two locks."""
    ok = "AAMkAGI2_x-y=="
    assert M.sanitize("graph_actions", "update-draft",
                      ["update-draft", "--message-id", ok, "--body", "x"]) is None
    for bad in ("x/../" + ok, ok + "%3D", ok + "/attachments", "a b", "", "a.b"):
        for argv in (["update-draft", "--message-id", bad, "--body", "x"],
                     ["update-draft", f"--message-id={bad}", "--body", "x"]):
            assert M.sanitize("graph_actions", "update-draft", argv), argv
    assert M.sanitize("gmail_client", "update-draft",
                      ["update-draft", "--email", "a@example.com", "--draft-id", "r-12/../r-3",
                       "--body", "x"])
    assert M.is_plain_id(ok) and not M.is_plain_id("a/b") and not M.is_plain_id(None)
    assert M.is_plain_id("A" * 3000, long=True) and not M.is_plain_id("A" * 3000)


@contextmanager
def _owner_private():
    """The owner-private directory this module seeded, whatever another
    test module in the same run set it to."""
    saved = os.environ.get("EA_PRIVATE_DIR")
    os.environ["EA_PRIVATE_DIR"] = str(_CALENDARS.parent)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("EA_PRIVATE_DIR", None)
        else:
            os.environ["EA_PRIVATE_DIR"] = saved


def test_a_repeated_option_is_refused_unless_it_appends():
    """argparse acts on the last use of a plain option and a check of argv
    may read another use, so a carve-out check could approve one
    --calendar-id while the child writes another. A second use of any
    option that does not append is refused, on both sides."""
    with _owner_private():
        _repeated_option_cases()


def _repeated_option_cases():
    cal = M.owner_ea_managed_calendar_id()
    assert cal
    create = ["create-event", "--email", "jo.ambrose4@example.com", "--summary", "Focus",
              "--start", "2026-07-20T09:00:00", "--end", "2026-07-20T10:00:00"]
    refused = [
        ("google_calendar_client", ["update-event", "--event-id", "E1",
                                    "--calendar-id", cal, "--calendar-id", "primary"]),
        ("google_calendar_client", ["delete-event", "--event-id", "E1",
                                    f"--calendar-id={cal}", "--calendar-id", "primary"]),
        ("google_calendar_client", create + ["--calendar-id", "primary", "--calendar-id", cal]),
        ("google_calendar_client", ["list-ea-managed", "--show-instances", "--show-instances"]),
        ("google_calendar_client", ["delete-event", "--event-id", "E1", "--event-id", "E2"]),
        ("graph_actions", ["search-mail", "--query", "a", "--query", "b"]),
        ("graph_actions", ["update-draft", "--message-id", "AAMkA=", "--message-id=AAMkB=",
                           "--body", "x"]),
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        for tool, argv in refused:
            assert "more than once" in (M.sanitize(tool, argv[0], argv) or ""), argv
            ran = []
            resp = _run_request(mm, tool, argv[0], argv,
                                lambda cmd: (ran.append(cmd), ("", 0))[1])
            # The verb check may refuse first (it reads the last value).
            assert resp["rc"] == 2 and "refused" in resp["stderr"], (argv, resp)
            assert ran == [], argv
        # The last --calendar-id is the EA-Managed one, so the verb check
        # passes and the repeat check is what refuses.
        last_is_ea = create + ["--calendar-id", "primary", "--calendar-id", cal]
        assert M.is_allowed("google_calendar_client", "create-event", last_is_ea, owner=True)
        resp = _run_request(mm, "google_calendar_client", "create-event", last_is_ea,
                            lambda cmd: ("", 0))
        assert resp["rc"] == 2 and "more than once" in resp["stderr"], resp
        # An append option repeats, as its parser defines it.
        series = create + ["--calendar-id", cal, "--recurrence", "RRULE:FREQ=DAILY;COUNT=2",
                           "--recurrence", "EXDATE:20260721T090000"]
        assert M.sanitize("google_calendar_client", "create-event", series) is None
        assert M.is_allowed("google_calendar_client", "create-event", series, owner=True)
    assert "--recurrence" in M.repeatable_options("google_calendar_client", "create-event")
    assert "--calendar-id" not in M.repeatable_options("google_calendar_client", "create-event")
    # The flag value the carve-out reads is the last one, the one argparse keeps.
    assert M._flag_value(["x", "--calendar-id", "a", "--calendar-id=b"], "--calendar-id") == "b"


def test_the_parser_scan_reads_which_options_repeat():
    src = '''
import argparse
def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    sub = ap.add_subparsers(dest="v")
    p = sub.add_parser("one", allow_abbrev=False)
    p.add_argument("--tag", action="append")
    p.add_argument("--flag", action="store_true")
    p.add_argument("--verbose", "-v", action="count")
    p.add_argument("--more", action="extend", nargs="+")
    p.add_argument("--plain")
    p.add_argument("--neg", action=argparse.BooleanOptionalAction)
'''
    opts, repeat = M._scan_parsers_full(src)
    assert set(opts["one"]) >= {"--tag", "--flag", "--verbose", "-v", "--more", "--plain", "--neg"}
    assert repeat["one"] == frozenset({"--tag", "--verbose", "-v", "--more"})


def test_the_mediator_takes_the_calendar_from_the_owner_file_only():
    """The resident's hint (its config, the environment here) never decides
    owner-side: the mediator checks the id in calendars.json, and its child
    runs with the owner-side marker so the client reads the same file."""
    with _owner_private():
        _owner_file_cases()


def _owner_file_cases():
    owner_cal = "pat.quillon11@example.com"
    hint = M.ea_managed_calendar_id()          # the resident's view
    assert hint != owner_cal
    saved = _CALENDARS.read_text()
    create = ["create-event", "--email", "jo.ambrose4@example.com", "--summary", "Focus",
              "--start", "2026-07-20T09:00:00", "--end", "2026-07-20T10:00:00"]
    try:
        _CALENDARS.write_text(json.dumps({"ea_managed_calendar_id": owner_cal}))
        assert M.is_allowed("google_calendar_client", "create-event",
                            create + ["--calendar-id", hint])          # the shim's view
        assert not M.is_allowed("google_calendar_client", "create-event",
                                create + ["--calendar-id", hint], owner=True)
        assert M.is_allowed("google_calendar_client", "create-event",
                            create + ["--calendar-id", owner_cal], owner=True)
        with tempfile.TemporaryDirectory() as td:
            tmp = _setup(td)
            mm = _mediator(tmp)
            envs = []
            real = mm.subprocess

            def run(cmd, **kw):
                envs.append(kw.get("env") or {})
                return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)
            mm.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
            try:
                for cal, runs in ((hint, False), (owner_cal, True)):
                    rid = uuid.uuid4().hex[:8]
                    (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
                        {"tool": "google_calendar_client", "verb": "create-event",
                         "argv": create + ["--calendar-id", cal]}))
                    before = len(envs)
                    mm.handle(str(M.QUEUE / f"req-{rid}.json"))
                    assert (len(envs) > before) is runs, cal
            finally:
                mm.subprocess = real
            assert envs and envs[-1].get(M.OWNER_SIDE_ENV) == "1"
        # No owner file: nothing is un-gated owner-side, whatever the hint says.
        _CALENDARS.unlink()
        assert not M.is_allowed("google_calendar_client", "delete-event",
                                ["delete-event", "--event-id", "E1"], owner=True)
        assert M.is_allowed("google_calendar_client", "delete-event",
                            ["delete-event", "--event-id", "E1"])
    finally:
        _CALENDARS.write_text(saved)
        os.chmod(_CALENDARS, 0o600)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


