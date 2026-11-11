"""scripts/boundary-audit.py: the daily read-only check of the boundary.

Pins the parts that decide a verdict, on scratch trees and a stand-in
resident (a uid that is neither this user nor in its groups), never the
host's install:

  * access as the resident from mode bits: search, read, replace (sticky
    directories included), the first weak point on a path through links,
    and the entries of a tree it can change;
  * the cron parser: writes into, runs from, works in and searches its PATH
    in resident space;
  * the token, auth-custody, private-dir and env checks on a scratch install;
  * the /proc check on scratch fstab and mountinfo files: not staged,
    staged but not active, live with the resident blind and the owner
    exempt, and each way that fails or warns;
  * the mediator probe: the verdict (a pass only when the mediator refuses
    every request), the requests, the decoy, and one round trip of the
    probe program through mail_mediator.handle() with its child stubbed;
  * alert on change: a post only when the set of non-passing checks or a
    detail differs, one post on recovery, class-level text only;
  * main: --dry-run neither posts nor saves, a failed post keeps the old
    state, and a FAIL exits 1.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# The audit reads Linux ownership and ACL rules; it is not ported to macOS.
pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux-only audit")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-boundary-audit-"))
os.environ["EA_DIR"] = str(EA_DIR)

_spec = importlib.util.spec_from_file_location("boundary_audit", ROOT / "scripts" / "boundary-audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules["boundary_audit"] = audit   # dataclasses look their module up
_spec.loader.exec_module(audit)

STRANGER = audit.Who(2 ** 31 - 7, frozenset({2 ** 31 - 7}))


@pytest.fixture
def base():
    """A directory every user can search (0755 under the sticky /tmp)."""
    if not os.path.isdir("/tmp") or (os.stat("/tmp").st_mode & 0o1777) != 0o1777:
        pytest.skip("no sticky world-writable /tmp")
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="ea-test-audit-"))
    d.chmod(0o755)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _file(path: Path, mode: int, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _dir(path: Path, mode: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _ctx(tree: Path, priv: Path, home: Path, who=STRANGER) -> "audit.Ctx":
    return audit.Ctx(ea_dir=tree, private=priv, owner_home=home, owner_logs=priv / "logs",
                     live_tree=tree / "no-live", lanes_tree=None, eabot_home=None,
                     resident=who, repo=ROOT)


# ── access as the resident ────────────────────────────────────────────

def test_reading_needs_search_on_every_directory_and_read_on_the_file(base):
    open_file = _file(base / "a" / "open.txt", 0o644)
    shut_file = _file(base / "a" / "shut.txt", 0o600)
    hidden = _file(_dir(base / "h", 0o700) / "open.txt", 0o644)
    assert audit.reads(STRANGER, open_file)
    assert not audit.reads(STRANGER, shut_file)
    assert not audit.reads(STRANGER, hidden)
    assert audit.reaches(STRANGER, str(base / "a")) and not audit.reaches(STRANGER, str(base / "h"))
    group = audit.Who(STRANGER.uid, frozenset({os.getegid()}))
    _dir(base / "g", 0o750)
    assert audit.reaches(group, str(base / "g")) and not audit.reaches(STRANGER, str(base / "g"))


def test_replacing_needs_write_on_the_parent_and_respects_the_sticky_bit(base):
    shared = _dir(base / "shared", 0o777)
    sticky = _dir(base / "sticky", 0o1777)
    closed = _dir(base / "closed", 0o755)
    _file(sticky / "mine.txt", 0o644)
    assert audit.can_replace(STRANGER, str(shared / "any"))
    assert not audit.can_replace(STRANGER, str(closed / "any"))
    assert not audit.can_replace(STRANGER, str(sticky / "mine.txt")), "someone else's entry"
    assert audit.can_replace(STRANGER, str(sticky / "free.txt")), "a name not yet taken"


def test_the_weak_point_is_found_through_a_link(base):
    target = _file(_dir(base / "w", 0o777) / "tool", 0o755)
    _dir(base / "fixed", 0o755)
    (base / "fixed" / "tool").symlink_to(target)
    assert "it can replace the entry" in audit.weak_point(STRANGER, base / "fixed" / "tool")
    assert audit.weak_point(STRANGER, _file(base / "fixed" / "own", 0o644)) is None
    assert "it can write it" in audit.weak_point(STRANGER, _file(base / "fixed" / "open", 0o666))
    owned = audit.Who(os.geteuid(), frozenset())
    assert audit.weak_point(owned, base / "fixed" / "own") is not None, "the owner can change its own"


def test_the_entries_the_resident_can_change(base):
    tree = _dir(base / "tree", 0o755)
    _file(tree / "fixed.py", 0o644)
    _file(tree / "open.py", 0o666)
    _dir(tree / "drop", 0o777)
    _dir(tree / "sub", 0o755)
    _file(tree / "sub" / "fixed.py", 0o644)
    got = [os.path.relpath(p, tree) for p in audit.changeable_entries(STRANGER, tree)]
    assert got == ["drop", "open.py"]
    shut = _dir(base / "shut", 0o700)
    _file(shut / "open.py", 0o666)
    assert audit.changeable_entries(STRANGER, shut) == [], "out of reach"


# ── cron ──────────────────────────────────────────────────────────────

CRONTAB = """\
SHELL=/bin/bash
PATH=/home/crh/ea-system/.venv/bin:/usr/bin:/bin
# a comment >> /home/ea-bot/x.log
*/5 * * * *  python3 /home/crh/ea-system/preprocessor/x.py check >> /home/crh/.ea-private/logs/x.log 2>&1
0 2 * * *    cd /home/crh/p && ./run.sh >> /home/crh/p/run.log 2>&1
"""


def _resident(path: str) -> bool:
    return path.startswith(("/home/ea-bot", "/home/crh/.ea/"))


def test_a_clean_crontab_has_no_finding():
    assert audit.cron_findings(CRONTAB, _resident) == []


@pytest.mark.parametrize("line, kind", [
    ("PATH=/home/ea-bot/bin:/usr/bin", "the cron PATH names a directory in resident space"),
    ("0 * * * * /home/crh/x.sh >> /home/crh/.ea/logs/x.log 2>&1", "a job writes its output into resident space"),
    ("0 * * * * /home/crh/x.sh 2> /home/ea-bot/err.log", "a job writes its output into resident space"),
    ("0 * * * * python3 -u /home/ea-bot/ea-system/tool.py", "a job runs code from resident space"),
    ("@daily /home/ea-bot/run.sh", "a job runs code from resident space"),
    ("0 * * * * cd /home/ea-bot/ea-system && ./run.sh", "a job works in resident space"),
    ("0 * * * * FOO=1 bash /home/ea-bot/x.sh", "a job runs code from resident space"),
])
def test_each_way_a_job_reaches_resident_space(line, kind):
    found = audit.cron_findings(CRONTAB + line + "\n", _resident)
    assert [k for k, _ in found] == [kind]


# ── the checks on a scratch install ───────────────────────────────────

def test_a_token_the_resident_can_read_fails_under_mediation(base, monkeypatch):
    tree = _dir(base / "ea", 0o755)
    auth = _dir(tree / "auth", 0o755)
    _file(auth / "msal_token_cache.json", 0o600, "{}")
    _file(auth / "scheduled-send-hmac.key", 0o600, "k" * 48)
    ctx = _ctx(tree, base / "priv", base)
    monkeypatch.setattr(audit, "_mediation_on", lambda c: True)
    assert [r["status"] for r in audit.check_tokens(ctx)] == ["PASS", "PASS"]
    (auth / "msal_token_cache.json").chmod(0o644)
    (auth / "scheduled-send-hmac.key").chmod(0o644)
    got = audit.check_tokens(ctx)
    assert [r["status"] for r in got] == ["FAIL", "FAIL"]
    monkeypatch.setattr(audit, "_mediation_on", lambda c: False)
    assert [r["status"] for r in audit.check_tokens(ctx)] == ["WARN", "FAIL"]


def test_the_auth_dir_and_the_tree_above_it_are_pinned(base):
    """deploy/pin-auth-dir.sh makes the EA tree and auth/ mount points, which
    no one can rename; without them the audit warns and names the script."""
    tree = _dir(base / "ea", 0o755)
    ctx = _ctx(tree, base / "priv", base)
    assert audit.check_auth_pinned(ctx, system="Linux")[0]["status"] == "PASS"   # no auth yet
    _dir(tree / "auth", 0o750)
    got = audit.check_auth_pinned(ctx, ismount=lambda p: False, system="Linux")
    assert got[0]["status"] == "WARN" and "pin-auth-dir.sh" in got[0]["fix"]
    got = audit.check_auth_pinned(ctx, ismount=lambda p: p == str(tree / "auth"), system="Linux")
    assert got[0]["status"] == "WARN" and str(tree) in got[0]["detail"]
    assert audit.check_auth_pinned(ctx, ismount=lambda p: True, system="Linux")[0]["status"] == "PASS"
    assert audit.check_auth_pinned(ctx, ismount=lambda p: False, system="Darwin")[0]["status"] == "PASS"
    assert audit.check_auth_pinned in audit.CHECKS


def test_the_pin_check_reads_the_kernel_mount_table():
    """A bind mount from the same file system has the parent's device
    number, so os.path.ismount() misses it; the check reads mountinfo."""
    import platform
    if platform.system() == "Linux":
        assert "/" in audit._mount_points()
    assert audit.check_auth_pinned.__defaults__[0] is audit._is_mount


def test_the_pin_check_follows_a_link_to_the_ea_tree(base):
    """~/.ea is a link to ~ea-bot/.ea on an install; ismount() is false for
    a link, so the check judges the real paths."""
    real = _dir(base / "bot-ea", 0o755)
    _dir(real / "auth", 0o750)
    (base / "link-ea").symlink_to(real)
    ctx = _ctx(base / "link-ea", base / "priv", base)
    mounts = {str(real.resolve()), str((real / "auth").resolve())}
    got = audit.check_auth_pinned(ctx, ismount=lambda p: p in mounts, system="Linux")
    assert got[0]["status"] == "PASS", got


def test_the_pin_script_binds_the_tree_then_auth_in_place():
    """Both directories are bound onto themselves, .ea first; nothing moves
    (a move after the .ea bind crosses mount points, so mv copies and the
    copy is a different directory)."""
    import subprocess
    script = Path(__file__).resolve().parents[1] / "deploy" / "pin-auth-dir.sh"
    out = subprocess.run(["bash", str(script), "--print"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "SUDO_USER": "owner-x",
                              "EA_PIN_EA_DIR": "/h/bot/.ea"}).stdout
    lines = [ln for ln in out.splitlines() if not ln.startswith("#")]
    assert lines == ["/h/bot/.ea /h/bot/.ea none rbind,nofail 0 0",
                     "/h/bot/.ea/auth /h/bot/.ea/auth none bind,nofail,"
                     "x-systemd.requires-mounts-for=/h/bot/.ea 0 0"]
    src = script.read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    # every move of auth/ or .ea is the resident's probe, never root's
    moves = [ln for ln in code.splitlines() if "mv -T" in ln]
    assert moves and all('sudo -n -u "$EA_BOT_USER" mv -T' in ln for ln in moves), moves
    assert "STORE" not in code
    # it refuses an auth/ that is not the owner's, and checks the result as the resident
    assert "never adopt it" in src and "probe ||" in src


def test_the_auth_dir_is_the_owners_and_nothing_in_it_the_residents(base):
    tree = _dir(base / "ea", 0o755)
    ctx = _ctx(tree, base / "priv", base)
    assert [r["status"] for r in audit.check_auth_custody(ctx)] == ["PASS"], "no auth dir yet"
    auth = _dir(tree / "auth", 0o750)
    _file(auth / "msal_token_cache.json", 0o600, "{}")
    _file(auth / "notes.txt", 0o640)
    _dir(auth / "profile", 0o700)
    _file(auth / "profile" / "Cookies", 0o600)
    got = audit.check_auth_custody(ctx)
    assert [r["status"] for r in got] == ["PASS"], got
    # A token the resident may write, through its group or as anyone.
    (auth / "msal_token_cache.json").chmod(0o606)
    got = audit.check_auth_custody(ctx)
    assert got[0]["status"] == "FAIL" and "msal_token_cache.json" in got[0]["detail"]
    (auth / "msal_token_cache.json").chmod(0o600)
    # A directory under auth/ the resident may write and search.
    (auth / "profile").chmod(0o707)
    got = audit.check_auth_custody(ctx)
    assert got[0]["status"] == "FAIL" and "profile" in got[0]["detail"]
    (auth / "profile").chmod(0o700)
    # Entries the resident owns: judged with a stand-in resident that is this
    # user, and an owner that is someone else, so the directory fails too.
    me = audit.Who(os.geteuid(), frozenset({os.getegid()}))
    got = audit.check_auth_custody(_ctx(tree, base / "priv", base, who=me), owner_uid=STRANGER.uid)
    assert got[0]["status"] == "FAIL" and "not the owner's" in got[0]["detail"]
    got = audit.check_auth_custody(_ctx(tree, base / "priv", base, who=me))
    assert got[0]["status"] == "FAIL" and "5 entries" in got[0]["detail"], got  # auth/ itself too
    # A link at the name is not the owner's directory.
    shutil.rmtree(auth)
    (tree / "auth").symlink_to(base)
    assert audit.check_auth_custody(ctx)[0]["status"] == "FAIL"


def test_the_auth_fix_lines_recover_and_never_launder(base):
    """A fix line never says to run the custody script over an auth dir the
    owner does not own, or to chown another user's file: either would make
    the resident's key or token the owner's. It says how to start again."""
    tree = _dir(base / "ea", 0o755)
    auth = _dir(tree / "auth", 0o750)
    me = audit.Who(os.geteuid(), frozenset({os.getegid()}))
    got = audit.check_auth_custody(_ctx(tree, base / "priv", base, who=me), owner_uid=STRANGER.uid)
    fix = got[0]["fix"]
    assert got[0]["status"] == "FAIL" and "nothing in it is trusted" in got[0]["detail"]
    assert "new scheduled-send key" in fix and "sign in again" in fix
    assert "never chown" in fix and "enable-mail-mediation" not in fix
    shutil.rmtree(auth)
    (tree / "auth").symlink_to(base)
    assert "new scheduled-send key" in audit.check_auth_custody(_ctx(tree, base / "priv", base))[0]["fix"]


def test_the_owners_quarantine_is_named_not_failed(base):
    tree = _dir(base / "ea", 0o755)
    auth = _dir(tree / "auth", 0o750)
    _file(auth / "msal_token_cache.json", 0o600, "{}")
    q = _dir(auth / ".quarantine-20260101000000-1", 0o700)
    _file(q / "scheduled-send-hmac.key", 0o666)       # would fail anywhere else under auth/
    got = audit.check_auth_custody(_ctx(tree, base / "priv", base))
    assert [r["status"] for r in got] == ["PASS", "WARN"], got
    assert ".quarantine-20260101000000-1" in got[1]["detail"] and "NOTE.txt" in got[1]["fix"]
    # Only an owner-only directory counts as a quarantine.
    q.chmod(0o755)
    got = audit.check_auth_custody(_ctx(tree, base / "priv", base))
    assert got[0]["status"] == "FAIL" and "scheduled-send-hmac.key" in got[0]["detail"]


def test_the_auth_check_runs_daily():
    assert audit.check_auth_custody in audit.CHECKS


def test_the_private_dir_must_be_out_of_reach(base):
    home = _dir(base / "home", 0o750)
    priv = _dir(home / ".ea-private", 0o700)
    _file(priv / "secrets.env", 0o600, "SLACK_BOT_TOKEN=k\n")
    ctx = _ctx(base / "ea", priv, home)
    assert audit.check_private_reach(ctx)[0]["status"] == "PASS"
    home.chmod(0o755)
    assert audit.check_private_reach(ctx)[0]["status"] == "WARN"
    priv.chmod(0o755)
    (priv / "secrets.env").chmod(0o644)
    got = audit.check_private_reach(ctx)[0]
    assert got["status"] == "FAIL" and "secrets.env" in got["detail"]
    home.chmod(0o777)
    priv.chmod(0o700)
    assert "replace" in audit.check_private_reach(ctx)[0]["detail"]


def test_an_owner_secret_in_the_shared_env_fails_and_names_only_the_key(base):
    tree = _dir(base / "ea", 0o755)
    ctx = _ctx(tree, base / "priv", base)
    assert audit.check_env_secrets(ctx)[0]["status"] == "PASS"
    _file(tree / ".env", 0o644, "EA_MODEL=opus\nTWOCAPTCHA_API_KEY=resident-value\n")
    assert audit.check_env_secrets(ctx)[0]["status"] == "PASS"
    _file(tree / ".env", 0o644, "EA_MODEL=opus\nSLACK_BOT_TOKEN=very-secret-value\n")
    got = audit.check_env_secrets(ctx)[0]
    assert got["status"] == "FAIL" and "SLACK_BOT_TOKEN" in got["detail"]
    assert "very-secret-value" not in json.dumps(got)


# ── ea-bot's Claude settings ──────────────────────────────────────────

def test_the_resident_claude_settings_warn_on_bypass_hooks_and_allow_rules(base, monkeypatch):
    home = _dir(base / "eabot", 0o755)
    ctx = _ctx(_dir(base / "ea", 0o755), base / "priv", base)
    ctx.eabot_home = str(home)
    read = []
    real = audit._read_shared
    monkeypatch.setattr(audit, "_read_shared", lambda p: read.append(p) or real(p))
    assert audit.check_claude_settings(ctx)[0]["status"] == "PASS"
    assert read == [home / ".claude" / "settings.json"], "read with the resident's access"
    settings = home / ".claude" / "settings.json"
    _file(settings, 0o644, json.dumps({"permissions": {"defaultMode": "auto"}, "model": "opus"}))
    assert audit.check_claude_settings(ctx)[0]["status"] == "PASS"
    for doc, word in (({"permissions": {"defaultMode": "bypassPermissions"}}, "bypassPermissions"),
                      ({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}},
                       "PreToolUse"),
                      ({"permissions": {"allow": ["Bash(curl *)"]}}, "permissions.allow")):
        _file(settings, 0o644, json.dumps(doc))
        got = audit.check_claude_settings(ctx)[0]
        assert got["status"] == "WARN" and word in got["detail"], doc
        assert "curl" not in got["detail"] and '"x"' not in got["detail"]
    _file(settings, 0o644, "{not json")
    assert audit.check_claude_settings(ctx)[0]["status"] == "WARN"
    ctx.eabot_home = None
    assert audit.check_claude_settings(ctx)[0]["status"] == "WARN"
    assert audit.check_claude_settings in audit.CHECKS


# ── /proc ─────────────────────────────────────────────────────────────

MOUNTINFO_OFF = ("22 1 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw\n"
                 "53 44 0:25 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw\n"
                 "54 53 0:26 / /proc/sys/fs/binfmt_misc rw,relatime shared:13 - autofs systemd-1 rw\n")
MOUNTINFO_ON = MOUNTINFO_OFF.replace("- proc proc rw", "- proc proc rw,gid=991,hidepid=invisible")
FSTAB_PLAIN = "/dev/disk/by-uuid/x / ext4 defaults 0 1\n# proc /proc proc hidepid=2,gid=5 0 0\n"
FSTAB_STAGED = FSTAB_PLAIN + ("# ea-proc-hidepid: ea-bot sees only its own processes\n"
                              "proc  /proc  proc  nosuid,nodev,noexec,relatime,hidepid=2,gid=991  0  0\n")
OWNER_HOLDS = {991}


def _proc_ctx(tmp_path, mountinfo: str, fstab: str, who=STRANGER):
    (tmp_path / "mountinfo").write_text(mountinfo)
    (tmp_path / "fstab").write_text(fstab)
    ctx = _ctx(tmp_path, tmp_path / "priv", tmp_path, who)
    ctx.fstab, ctx.mountinfo = tmp_path / "fstab", tmp_path / "mountinfo"
    return ctx


def _proc(ctx, sees=False, held=OWNER_HOLDS) -> dict:
    asked = []
    got = audit.check_proc(ctx, probe=lambda pid: asked.append(pid) or sees, held_gids=held)
    assert len(got) == 1
    return {**got[0], "asked": asked}


def test_the_proc_options_come_from_the_mount_and_the_first_active_fstab_line():
    assert audit.proc_mount_options(MOUNTINFO_OFF) == {
        "rw": "", "nosuid": "", "nodev": "", "noexec": "", "relatime": ""}
    on = audit.proc_mount_options(MOUNTINFO_ON)
    assert audit._hidepid(on) == "invisible" and audit._proc_gid(on) == 991
    older = audit.proc_mount_options(MOUNTINFO_OFF.replace("- proc proc rw", "- proc proc rw,hidepid=2"))
    assert audit._hidepid(older) == "invisible" and audit._proc_gid(older) is None
    assert audit.proc_mount_options(MOUNTINFO_OFF.splitlines()[0]) is None
    assert audit.fstab_proc_options(FSTAB_PLAIN) is None, "a comment is not a mount"
    staged = audit.fstab_proc_options(FSTAB_STAGED)
    assert audit._hidepid(staged) == "invisible" and audit._proc_gid(staged) == 991


def test_an_unstaged_proc_is_a_warning(tmp_path):
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_OFF, FSTAB_PLAIN))
    assert got["status"] == "WARN" and got["detail"].startswith("not staged")
    assert "stage-proc-hidepid.sh" in got["fix"]
    assert got["asked"] == [], "nothing runs as the resident until the option is live"


def test_a_staged_line_reports_staged_not_active_until_the_boot(tmp_path):
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_OFF, FSTAB_STAGED))
    assert got["status"] == "WARN" and got["detail"].startswith("staged, not active")
    assert "gid=991" in got["detail"] and got["fix"] == "reboot"
    assert got["asked"] == []
    holder = audit.Who(STRANGER.uid, frozenset({991}))
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_OFF, FSTAB_STAGED, who=holder))
    assert got["status"] == "WARN" and "hides nothing" in got["detail"]


def test_the_live_option_passes_when_the_resident_is_blind_and_the_owner_is_not(tmp_path):
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_STAGED))
    assert got["status"] == "PASS", got["detail"]
    assert got["asked"] == [os.getpid()], "the resident child is asked about this owner process"
    assert "could not open" in got["detail"]
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_STAGED), sees=None)
    assert got["status"] == "PASS" and "no resident child" in got["detail"]


def test_a_resident_that_sees_an_owner_process_fails(tmp_path):
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_STAGED), sees=True)
    assert got["status"] == "FAIL" and "opened the /proc entry" in got["detail"]
    assert str(os.getpid()) not in got["detail"], "a stable detail: one post, not one per run"
    holder = audit.Who(STRANGER.uid, frozenset({991}))
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_STAGED, who=holder))
    assert got["status"] == "FAIL" and "holds the exemption gid 991" in got["detail"]


def test_hidepid_noaccess_still_lists_every_pid(tmp_path):
    mountinfo = MOUNTINFO_ON.replace("hidepid=invisible", "hidepid=noaccess")
    assert _proc(_proc_ctx(tmp_path, mountinfo, FSTAB_STAGED))["status"] == "FAIL"
    mountinfo = MOUNTINFO_ON.replace("hidepid=invisible", "hidepid=ptraceable")
    assert _proc(_proc_ctx(tmp_path, mountinfo, FSTAB_STAGED))["status"] == "WARN"


def test_the_owner_without_the_gid_or_a_dropped_fstab_line_is_a_warning(tmp_path):
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_STAGED), held={1000})
    assert got["status"] == "WARN" and "does not hold gid 991" in got["detail"]
    got = _proc(_proc_ctx(tmp_path, MOUNTINFO_ON, FSTAB_PLAIN))
    assert got["status"] == "WARN" and "next boot drops it" in got["detail"]
    no_gid = MOUNTINFO_ON.replace("gid=991,", "")
    got = _proc(_proc_ctx(tmp_path, no_gid, FSTAB_STAGED.replace(",gid=991", "")))
    assert got["status"] == "WARN" and "no gid= exemption" in got["detail"]


def test_a_check_that_crashes_is_a_warning():
    def broken(ctx):
        raise RuntimeError("no answer")
    got = audit.run_checks(None, [broken])
    assert got[0]["status"] == "WARN" and "broken" in got[0]["name"]


# ── alert on change ───────────────────────────────────────────────────

def _r(status, name="tokens: out of the resident's reach", detail="/home/crh/.ea/auth/x mode 0644"):
    return audit.res(status, name, detail)


def test_a_post_only_when_the_result_changes():
    clean = [_r("PASS")]
    text, state = audit.decide({}, clean)
    assert text is None and state["fingerprint"] == []
    text, bad = audit.decide(state, [_r("FAIL")])
    assert text and "FAIL tokens: out of the resident's reach" in text
    assert audit.decide(bad, [_r("FAIL")])[0] is None, "unchanged"
    assert audit.decide(bad, [_r("FAIL", detail="another file")])[0], "a new problem in the same check"
    text, _ = audit.decide(bad, clean)
    assert "passes again" in text


def test_the_post_names_checks_and_statuses_only():
    text, _ = audit.decide({}, [_r("FAIL"), _r("WARN", "sudo: x", "the policy refuses /usr/bin/python3")])
    assert "/home/crh" not in text and "python3" not in text and "0644" not in text
    assert "1 FAIL, 1 WARN" in text


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """main() on fake checks, a scratch state file, and a recorded post."""
    posts = []
    monkeypatch.setattr(audit, "STATE_FILE", tmp_path / "state" / "state.json")
    monkeypatch.setattr(audit.Ctx, "live", classmethod(lambda cls, ea_dir: None))
    monkeypatch.setattr(audit, "_post", lambda text: posts.append(text) or True)
    return posts


def test_dry_run_neither_posts_nor_saves(isolated, monkeypatch, capsys):
    monkeypatch.setattr(audit, "CHECKS", [lambda ctx: [_r("FAIL")]])
    assert audit.main(["--dry-run"]) == 1
    assert isolated == [] and not audit.STATE_FILE.exists()
    assert "would post" in capsys.readouterr().out


def test_a_run_posts_once_and_saves_its_state(isolated, monkeypatch):
    monkeypatch.setattr(audit, "CHECKS", [lambda ctx: [_r("WARN")]])
    assert audit.main([]) == 0
    assert audit.main([]) == 0
    assert len(isolated) == 1
    assert oct(audit.STATE_FILE.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(audit, "CHECKS", [lambda ctx: [_r("PASS")]])
    assert audit.main(["--json"]) == 0
    assert len(isolated) == 2 and "passes again" in isolated[-1]


def test_a_failed_post_keeps_the_old_state(isolated, monkeypatch):
    monkeypatch.setattr(audit, "CHECKS", [lambda ctx: [_r("FAIL")]])
    monkeypatch.setattr(audit, "_post", lambda text: False)
    assert audit.main([]) == 1
    assert not audit.STATE_FILE.exists()


# ── ssh ──────────────────────────────────────────────────────────────

def _ssh(monkeypatch, tmp_path, dropin: str | None, include: bool = True):
    d = tmp_path / "01-ea-deny-resident.conf"
    if dropin is not None:
        d.write_text(dropin)
    main = tmp_path / "sshd_config"
    main.write_text(("Include /etc/ssh/sshd_config.d/*.conf\n" if include else "") + "Port 22\n")
    monkeypatch.setattr(audit, "SSH_DROPIN", d)
    monkeypatch.setattr(audit, "SSHD_CONFIG", main)
    (r,) = audit.check_ssh(None)
    return r


def test_ssh_warns_until_staged(monkeypatch, tmp_path):
    r = _ssh(monkeypatch, tmp_path, None)
    assert r["status"] == "WARN" and "not staged" in r["detail"] and "ssh-deny-resident" in r["fix"]


def test_ssh_passes_when_the_resident_is_denied(monkeypatch, tmp_path):
    r = _ssh(monkeypatch, tmp_path, "# note\nDenyUsers ea-bot\n")
    assert r["status"] == "PASS"


def test_ssh_fails_on_a_dropin_that_does_not_deny(monkeypatch, tmp_path):
    assert _ssh(monkeypatch, tmp_path, "# DenyUsers ea-bot\nDenyUsers other\n")["status"] == "FAIL"


def test_ssh_fails_when_sshd_ignores_the_dropins(monkeypatch, tmp_path):
    assert _ssh(monkeypatch, tmp_path, "DenyUsers ea-bot\n", include=False)["status"] == "FAIL"


# ── the mediator probe ────────────────────────────────────────────────

REFUSED = {"rc": 2, "stderr": '{"status": "error", "message": "mediator refused: file path"}'}


def test_the_probe_passes_only_when_the_mediator_refuses_every_request():
    reqs = audit.mediator_probe_requests("/home/owner/.local/state/x/mediator-decoy")
    status, detail = audit.judge_mediator_probe(reqs, {str(r["id"]): REFUSED for r in reqs})
    assert status == "PASS" and "--body-fi" in detail
    read = {"rc": 1, "stderr": '{"error": "Empty body ..."}'}
    status, detail = audit.judge_mediator_probe(
        reqs, {"0": REFUSED, "1": REFUSED, "2": read, "3": {"rc": 0, "stderr": ""}})
    assert status == "FAIL"
    assert "--body-fi: the owner-side tool read the decoy" in detail
    assert "--body-f=: the owner-side tool ran (rc 0)" in detail
    status, detail = audit.judge_mediator_probe(reqs, {"0": REFUSED, "1": {"result": "withdrawn"}})
    assert status == "WARN" and "--body-file= (withdrawn)" in detail and "no answer" in detail


def test_each_probe_request_names_the_decoy_as_its_body_file_only():
    """One verb, the one whose body file is read before any mail API call;
    the decoy is the last argument, and no other argument is a path."""
    reqs = audit.mediator_probe_requests("/d/decoy")
    assert {(r["tool"], r["verb"], r["argv"][0]) for r in reqs} == {
        ("graph_actions", "create-draft", "create-draft")}
    for r in reqs:
        assert r["argv"][-1].endswith("/d/decoy")
        assert [a for a in r["argv"] if "/" in a] == [r["argv"][-1]]
        assert r["argv"][r["argv"].index("--body") + 1] == "x"


def test_the_decoy_is_an_empty_owner_only_file(tmp_path):
    path = tmp_path / "state" / "mediator-decoy"
    assert audit._mediator_decoy(path) == str(path)
    assert path.stat().st_size == 0 and (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    path.write_text("not empty")
    assert audit._mediator_decoy(path) == str(path) and path.stat().st_size == 0
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    assert audit._mediator_decoy(path) is None and not (tmp_path / "elsewhere").exists()


def test_the_probe_program_round_trips_through_the_mediator(tmp_path, monkeypatch):
    """MEDIATOR_PROBE, run as this user, against mail_mediator.handle() with
    its child stubbed: every request is read by the mediator and answered,
    each answer is removed, and the exact spelling is refused."""
    import subprocess
    import threading
    import types
    import mail_mediation as M
    import mail_mediator as MM
    ea = tmp_path / "ea"
    for sub in ("config", "state/mail-queue", "logs"):
        (ea / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "EA_DIR", ea)
    monkeypatch.setattr(M, "QUEUE", ea / "state" / "mail-queue")
    monkeypatch.setattr(MM, "AUDIT", ea / "logs" / "mail-mediator.log")
    monkeypatch.setattr(MM, "STAGING_ROOT", tmp_path / "staging")
    # /tmp is an allowed root and holds this test's decoy; leave it out.
    monkeypatch.setattr(M, "_allowed_roots", lambda: [ea / "state"])
    monkeypatch.setattr(MM, "subprocess", types.SimpleNamespace(
        run=lambda cmd, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=1),
        TimeoutExpired=subprocess.TimeoutExpired))
    decoy = audit._mediator_decoy(tmp_path / "owner" / "mediator-decoy")
    reqs = audit.mediator_probe_requests(decoy)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            for req in sorted(M.QUEUE.glob("req-*.json")):
                MM.handle(str(req))
            stop.wait(0.05)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    spec = {"queue": str(M.QUEUE), "timeout": 20,
            "requests": [{k: r[k] for k in ("id", "tool", "verb", "argv")} for r in reqs]}
    try:
        p = subprocess.run([sys.executable, "-I", "-c", audit.MEDIATOR_PROBE, json.dumps(spec)],
                           capture_output=True, timeout=60)
    finally:
        stop.set()
        t.join(5)
    assert p.returncode == 0, p.stderr
    answers = json.loads(p.stdout)
    assert set(answers) == {str(r["id"]) for r in reqs}
    assert all("rc" in a for a in answers.values()), answers
    assert answers["0"]["rc"] == 2 and "mediator refused" in answers["0"]["stderr"]
    assert list(M.QUEUE.iterdir()) == []
