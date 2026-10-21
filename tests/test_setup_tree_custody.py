"""Root's steps on ea-bot's trees and on the owner's auth directory.

scripts/install/setup-ea-user.sh, scripts/install/setup-ea-user-macos.sh and deploy/enable-mail-mediation.sh
run as root over ea-bot space, where ea-bot can rename or replace any entry
(docs/SECURITY.md "Owner trust rules"). deploy/tree-custody.sh holds the
rules they share:

- the auth directory and every token in it are never ea-bot's, at any
  moment: the directory goes to the owner first, then each credential
  becomes the owner's alone, and modes are only tightened;
- root adopts nothing: an auth directory neither the owner nor root owns
  stops the run with the recovery steps, and an entry the owner does not
  own, or a file with more than one link, is quarantined, never chowned;
- root changes no mode in a tree ea-bot controls: mode and group fixes run
  as ea-bot, which can change only what it owns;
- root hands ownership to ea-bot only in a tree moved in from the owner's
  home that run, entries before their directory, each chown made in the
  directory find opened and on a link itself, auth/ and .env.secrets aside;
- the mediation enabler locks the tokens before anything else, and no
  failure or rollback gives one back to ea-bot.

Each test cuts the steps out of a script and runs them in a temp directory
with the script's variables pointed there. Stubs for chown, chgrp and sudo
record each call with the directory it ran in and the user sudo would
have switched to; chmod is recorded and then run, as this user, on files
this user owns. Nothing runs as root, and no path outside the temp
directory is touched.
"""
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="the stubs use GNU find")

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh"]
REAL_CHMOD = shutil.which("chmod") or "/bin/chmod"
RESIDENT_UID = 2 ** 31 - 7          # a stand-in ea-bot that is not this user
RESIDENT_GID = 2 ** 31 - 9
ME = os.getuid()                    # the owner in every world, unless a test says otherwise
STRANGER_UID = 2 ** 31 - 11         # an owner uid that owns nothing here

RECORD = """#!/usr/bin/env bash
# Stands in for {name}: records who would run it, where, and its arguments.
printf '%s|%s|%s|%s\\n' {name} "${{AS_USER:-root}}" "$(pwd -P)" "$*" >> "$CALL_LOG"
"""
CHMOD_STUB = RECORD.format(name="chmod") + f'exec {shlex.quote(REAL_CHMOD)} "$@"\n'
SUDO_STUB = """#!/usr/bin/env bash
# Stands in for sudo -u USER CMD: records the switch, then runs CMD as this
# user with AS_USER set, so the stubs below it record USER.
user=
while [ $# -gt 0 ]; do
    case "$1" in
        -u) user=$2; shift 2 ;;
        -n) shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done
printf 'sudo|%s|%s|%s\\n' "$user" "$(pwd -P)" "$*" >> "$CALL_LOG"
AS_USER=$user exec "$@"
"""


def _cut(script: str, start: str, end: str) -> str:
    text = (REPO / script).read_text()
    i = text.index(start)
    return text[i:text.index(end, i)]


class World:
    """An owner home, ea-bot's home and the stubs, under one temp dir."""

    def __init__(self, root: Path, script: str):
        self.root = root.resolve()
        self.script = script
        self.owner_home = self.root / "owner-home"
        self.home = self.root / "ea-bot-home"
        self.ea = self.home / ".ea"
        self.old = self.owner_home / ".ea"
        self.log = self.root / "calls.log"
        self.owner_home.mkdir()
        self.home.mkdir(mode=0o755)
        stubs = self.root / "stubs"
        stubs.mkdir()
        for name, body in (("chown", RECORD.format(name="chown")),
                           ("chgrp", RECORD.format(name="chgrp")),
                           ("chmod", CHMOD_STUB), ("sudo", SUDO_STUB)):
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        self.path = f"{stubs}:/usr/bin:/bin"

    def tree(self, at: Path) -> Path:
        """A state tree as scripts/install/setup.sh leaves it, with the owner's tokens."""
        for d in ("auth", "state/sub", "config", "logs", "exports"):
            (at / d).mkdir(parents=True)
        files = {"auth/msal_token_cache.json": 0o640, "auth/google_token_a_at_b_c.json": 0o660,
                 "auth/google_client_secret.json": 0o644, "auth/scheduled-send-hmac.key": 0o640,
                 "auth/notes.txt": 0o664, ".env": 0o644, ".env.secrets": 0o644,
                 "state/ledger.json": 0o600, "state/sub/x.json": 0o600,
                 "config/ea-config.json": 0o600, "CLAUDE.md": 0o644}
        for rel, mode in files.items():
            (at / rel).write_text("x")
            (at / rel).chmod(mode)
        (at / "auth" / "profile").mkdir()
        (at / "auth" / "profile" / "Cookies").write_text("c")
        return at

    def run(self, resident_uid: int = RESIDENT_UID, step6: bool = False,
            owner_uid: int | None = None, inject: str = ""):
        owner_uid = os.getuid() if owner_uid is None else owner_uid
        assigns = {"REAL_USER": "owner", "REAL_UID": str(owner_uid), "REAL_GROUP": "ownergrp",
                   "REAL_HOME": str(self.owner_home), "SCRIPT_DIR": str(REPO),
                   "EA_BOT_HOME": str(self.home), "EA_BOT_EA_DIR": str(self.ea),
                   "OLD_EA_DIR": str(self.old), "EA_BOT_UID": str(resident_uid),
                   "EA_GID": str(RESIDENT_GID)}
        body = _cut(self.script, "# ── Step 4:", "# Create backward-compat symlink"
                    if "macos" not in self.script else "# Back-compat symlink")
        if step6:
            body += _cut(self.script, "# ── Step 6:", "# ── Step 6b:")
        if inject:                  # after the helper is sourced, so it can stand in for one
            src = '. "$SCRIPT_DIR/deploy/tree-custody.sh"\n'
            assert body.count(src) == 1
            body = body.replace(src, src + inject)
        script = ("set -euo pipefail\n"
                  + "".join(f"{k}={shlex.quote(v)}\n" for k, v in assigns.items())
                  + body + "echo steps-done\n")
        env = {"PATH": self.path, "CALL_LOG": str(self.log), "HOME": str(self.root),
               "LC_ALL": "C"}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=env, cwd=str(self.root), timeout=120)

    def calls(self, kind: str | None = None) -> list[tuple[str, str, str, str]]:
        rows = [tuple(ln.split("|", 3)) for ln in self.log.read_text().splitlines()] \
            if self.log.exists() else []
        return [r for r in rows if kind is None or r[0] == kind]


def _targets(cwd: str, args: str) -> list[Path]:
    """The paths a recorded chown or chmod names, from the directory it ran in."""
    return [Path(os.path.normpath(os.path.join(cwd, a)))
            for a in shlex.split(args)[1:] if not a.startswith("-")]


def _under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _foreign(*names: str) -> str:
    """Shell that stands in for the ownership probes: an entry with one of
    `names` reads as another user's (uid 4242), so a test can hold such an
    entry without root. Everything else is read from the real file."""
    pat = "|".join(shlex.quote(n) for n in names)
    return (
        f'tc_uid() {{ case "${{1##*/}}" in {pat}) echo 4242 ;; '
        '*) ls -lnd -- "$1" | awk \'NR == 1 {print $3}\' ;; esac; }\n'
        'tc_foreign_in() { find -P "$1" \\( ! -uid "$2" -o \\( ! -type d -links +1 \\) \\) -print; '
        'find -P "$1" -print | while IFS= read -r p; do '
        f'case "${{p##*/}}" in {pat}) echo "$p" ;; esac; done; }}\n')


def _quarantine(auth: Path) -> Path:
    found = sorted(auth.glob(".quarantine-*"))
    assert len(found) == 1, found
    return found[0]


def _auth_chowns(world, auth: Path) -> list[str]:
    """The arguments of every chown made in auth/ or below."""
    return [args for _, _, cwd, args in world.calls("chown") if _under(Path(cwd), auth)]


@pytest.fixture(params=SCRIPTS)
def world(request, tmp_path):
    return World(tmp_path, request.param)


# ── the setup scripts ─────────────────────────────────────────────────

def test_a_first_move_gives_ea_bot_everything_but_auth_and_the_secrets(world):
    world.tree(world.old)
    r = world.run()
    assert r.returncode == 0 and "steps-done" in r.stdout, r.stderr
    ea = world.ea.resolve()
    auth = ea / "auth"
    given = [(cwd, args) for _, who, cwd, args in world.calls("chown") if f"{RESIDENT_UID}:" in args]
    assert given, "the moved tree goes to ea-bot"
    named = [t for cwd, args in given for t in _targets(cwd, args)]
    assert not [t for t in named if _under(t, auth) or t == ea / ".env.secrets"], named
    assert all(args.startswith("-h ") for _, args in given), "a link is changed itself"
    assert ea in named and ea / "state" / "sub" / "x.json" in named and ea / "CLAUDE.md" in named
    # Entries before their directory: the walk reaches state/ after state/sub/x.json.
    order = [t for t in named]
    assert order.index(ea / "state" / "sub" / "x.json") < order.index(ea / "state") < order.index(ea)


def test_the_auth_dir_is_the_owners_before_any_entry_is_given_away(world):
    world.tree(world.old)
    assert world.run().returncode == 0
    auth = world.ea.resolve() / "auth"
    chowns = world.calls("chown")
    first = chowns[0]
    assert first[2] == str(auth) and shlex.split(first[3]) == ["owner:ea", "."]
    owner_only = {n: shlex.split(a)[0] for _, _, cwd, a in chowns if cwd == str(auth)
                  for n in [shlex.split(a)[-1]]}
    for name in ("./msal_token_cache.json", "./google_token_a_at_b_c.json",
                 "./google_client_secret.json", "./scheduled-send-hmac.key"):
        assert owner_only[name] == "owner:ownergrp", name
    assert owner_only["./notes.txt"] == "owner:ea"
    assert owner_only["./profile"] in ("owner:ownergrp", "-R")
    # Modes only tightened, and every credential the owner's alone.
    assert _mode(auth) == 0o750
    for name in ("msal_token_cache.json", "google_token_a_at_b_c.json",
                 "google_client_secret.json", "scheduled-send-hmac.key"):
        assert _mode(auth / name) == 0o600, name
    assert _mode(auth / "notes.txt") == 0o640, "group write and other access taken away"
    assert _mode(auth / "profile") == 0o700 and _mode(auth / "profile" / "Cookies") & 0o077 == 0


def test_root_changes_no_mode_outside_auth(world):
    world.tree(world.old)
    assert world.run(step6=True).returncode == 0
    auth = world.ea.resolve() / "auth"
    for _, who, cwd, args in world.calls("chmod"):
        if who == "root":
            assert all(_under(t, auth) for t in _targets(cwd, args)), (cwd, args)
    as_bot = [args for _, who, _, args in world.calls("sudo") if who == "ea-bot"]
    assert any(a.startswith("chmod 750 ") for a in as_bot)
    assert any(a.startswith("mkdir -p ") and a.endswith("/.claude") for a in as_bot)
    assert any(a.startswith("chmod 710 ") for a in as_bot)
    secrets = [args for _, who, _, args in world.calls("sudo") if who == "owner"]
    assert secrets == [f"chmod 600 {world.ea}/.env.secrets"]


def test_a_rerun_hands_nothing_to_ea_bot(world):
    world.tree(world.ea)
    # ea-bot renamed the locked auth dir away and made a fresh one: the real
    # tokens now sit in its state tree under another name.
    (world.ea / "auth").rename(world.ea / "state" / "x")
    (world.ea / "auth").mkdir()
    r = world.run()
    assert r.returncode == 0, r.stderr
    assert not [c for c in world.calls("chown") if f"{RESIDENT_UID}:" in c[3]]
    assert "are not ea-bot's" in r.stdout


def test_a_rerun_stops_at_an_auth_dir_the_owner_does_not_own(world):
    """ea-bot renamed the owner's auth/ away and made its own, with a key it
    knows: the run adopts nothing in it and says how to recover."""
    world.tree(world.ea)
    r = world.run(owner_uid=STRANGER_UID)
    assert r.returncode != 0 and "steps-done" not in r.stdout
    assert "Never chown a file from the old directory to owner" in r.stderr
    assert "make a new scheduled-send key" in r.stderr and "own backup" in r.stderr
    auth = world.ea.resolve() / "auth"
    assert _auth_chowns(world, auth) == []
    assert not [c for c in world.calls("chmod") if _under(Path(c[2]), auth)]
    assert _mode(auth / "scheduled-send-hmac.key") == 0o640, "nothing in it was touched"


def test_a_first_move_also_adopts_nothing_another_user_owns(world):
    world.tree(world.old)
    r = world.run(owner_uid=STRANGER_UID)
    assert r.returncode != 0 and "steps-done" not in r.stdout
    assert not [c for c in world.calls("chown") if f"{RESIDENT_UID}:" in c[3]]
    assert _auth_chowns(world, world.ea.resolve() / "auth") == []


def test_an_entry_the_owner_does_not_own_is_quarantined_not_chowned(world):
    world.tree(world.ea)
    auth = world.ea.resolve() / "auth"
    os.link(auth / "notes.txt", world.root / "notes-elsewhere")      # a second name, outside auth/
    r = world.run(inject=_foreign("scheduled-send-hmac.key", "google_token_a_at_b_c.json", "Cookies"))
    assert r.returncode == 0 and "steps-done" in r.stdout, r.stderr
    q = _quarantine(auth)
    assert _mode(q) == 0o700
    moved = {"scheduled-send-hmac.key", "google_token_a_at_b_c.json", "profile", "notes.txt"}
    assert {p.name for p in q.iterdir()} == moved | {"NOTE.txt"}
    assert not [n for n in moved if os.path.lexists(auth / n)]
    assert _mode(q / "scheduled-send-hmac.key") == 0o640, "a quarantined file keeps its mode"
    chowns = _auth_chowns(world, auth)
    for name in moved:
        assert not [a for a in chowns if a.endswith(f"./{name}")], (name, chowns)
    assert not [a for a in chowns if a.startswith("-R ")]
    assert f"owner:ownergrp {q.name}" in chowns and f"owner:ownergrp {q.name}/NOTE.txt" in chowns
    note = (q / "NOTE.txt").read_text()
    assert "scheduled-send-hmac.key: was owned by uid 4242" in note
    assert "notes.txt: had 2 links" in note and "profile: held an entry" in note
    assert "never move one" in note and "new scheduled-send key" in note
    # The owner's own token stays, owner-only.
    assert _mode(auth / "msal_token_cache.json") == 0o600
    assert "owner:ownergrp ./msal_token_cache.json" in chowns


def test_modes_are_set_as_ea_bot_and_never_through_a_link(world, tmp_path):
    world.tree(world.ea)
    victim = tmp_path / "victim"
    victim.write_text("v")
    victim.chmod(0o600)
    (world.ea / "state" / "planted").symlink_to(victim)
    (world.ea / "config" / "planted").symlink_to(victim)
    (world.ea / "auth" / "msal_token_cache.json").unlink()
    (world.ea / "auth" / "msal_token_cache.json").symlink_to(victim)
    r = world.run(resident_uid=os.getuid())       # ea-bot owns the tree here
    assert r.returncode == 0, r.stderr
    assert _mode(victim) == 0o600, "nothing followed a link"
    assert "is a symbolic link; left as it is" in r.stderr
    assert _mode(world.ea / "state" / "sub") == 0o2775
    assert _mode(world.ea / "state" / "ledger.json") == 0o664
    assert _mode(world.ea / "config" / "ea-config.json") == 0o640
    assert _mode(world.ea / ".env") == 0o640


def test_a_link_at_the_state_dir_is_refused_before_any_change(world, tmp_path):
    elsewhere = world.tree(tmp_path / "elsewhere")
    world.ea.symlink_to(elsewhere)
    world.old.symlink_to(world.ea)
    r = world.run()
    assert r.returncode != 0 and "steps-done" not in r.stdout
    assert world.calls("chown") == [] and world.calls("chmod") == []
    assert _mode(elsewhere / "auth" / "msal_token_cache.json") == 0o640


def test_every_mode_or_owner_change_in_ea_bot_space_is_a_helper_or_runs_as_a_user():
    """In Steps 4 and 6 root calls chmod, chown and chgrp only through the
    helper (auth_lock, tc_give_moved_tree); every other call runs as ea-bot
    or the owner."""
    for script in SCRIPTS:
        end = "# Back-compat symlink" if "macos" in script else "# Create backward-compat symlink"
        steps = _cut(script, "# ── Step 4:", end) + _cut(script, "# ── Step 6:", "# ── Step 6b:")
        for ln in steps.replace("\\\n", " ").splitlines():
            code = ln.split("#", 1)[0] if not ln.lstrip().startswith("echo") else ""
            if any(f"{c} " in code for c in ("chmod", "chown", "chgrp")):
                assert "tc_as_user " in code, (script, ln)
        assert '. "$SCRIPT_DIR/deploy/tree-custody.sh"' in steps, script
        assert "for dir in config auth" not in steps and "chown -R" not in steps, script


# ── the helper on its own ─────────────────────────────────────────────

def _helper(body: str, cwd: Path, log: Path, path: str):
    script = f". {shlex.quote(str(REPO / 'deploy' / 'tree-custody.sh'))}\n{body}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=str(cwd),
                          env={"PATH": path, "CALL_LOG": str(log), "LC_ALL": "C"}, timeout=60)


def test_auth_lock_refuses_a_name_that_leads_elsewhere(world, tmp_path):
    real = world.tree(tmp_path / "real") / "auth"
    (world.home / "auth").symlink_to(real)
    r = _helper(f'auth_lock {shlex.quote(str(world.home.resolve() / "auth"))} owner {ME} ea ownergrp',
                world.root, world.log, world.path)
    assert r.returncode == 1 and "not a real directory" in r.stderr
    assert world.calls() == []


def test_auth_lock_has_nothing_to_do_without_an_auth_dir(world):
    r = _helper(f'auth_lock {shlex.quote(str(world.home.resolve() / "auth"))} owner {ME} ea ownergrp',
                world.root, world.log, world.path)
    assert r.returncode == 0 and world.calls() == []


def test_the_ownership_probes_read_the_files_themselves(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "f").write_text("x")
    (d / "sub" / "g").write_text("y")
    (d / "link").symlink_to("/")
    body = ('for p in d/f d/link d/sub; do echo "$p $(tc_uid "$p") $(tc_links "$p")"; done\n'
            f'echo mine; tc_foreign_in d {ME}; echo theirs; tc_foreign_in d {STRANGER_UID}\n')
    out = _helper(body, tmp_path, tmp_path / "log", "/usr/bin:/bin").stdout
    assert f"d/f {ME} 1" in out and f"d/link {ME} 1" in out
    mine, theirs = out.split("mine\n")[1].split("theirs\n")
    assert mine == "", "the owner's own tree, one link each"
    assert set(theirs.split()) == {"d", "d/f", "d/sub", "d/sub/g", "d/link"}
    os.link(d / "sub" / "g", tmp_path / "g2")
    out = _helper(body, tmp_path, tmp_path / "log", "/usr/bin:/bin").stdout
    assert out.split("mine\n")[1].split("theirs\n")[0].split() == ["d/sub/g"]


def test_auth_lock_quarantines_a_hard_link_with_no_stand_in(world):
    """No probe replaced: a token with a second name is moved, not chowned."""
    world.tree(world.ea)
    auth = world.ea.resolve() / "auth"
    os.link(auth / "msal_token_cache.json", world.root / "elsewhere")
    r = _helper(f"auth_lock {shlex.quote(str(auth))} owner {ME} ea ownergrp",
                world.root, world.log, world.path)
    assert r.returncode == 0, r.stderr
    q = _quarantine(auth)
    assert (q / "msal_token_cache.json").exists() and not (auth / "msal_token_cache.json").exists()
    assert "had 2 links" in r.stderr
    assert _mode(world.root / "elsewhere") == 0o640, "the other name kept its mode"
    assert not [a for a in _auth_chowns(world, auth) if a.endswith("./msal_token_cache.json")]


def test_auth_lock_refuses_a_directory_the_owner_does_not_own(world):
    world.tree(world.ea)
    auth = world.ea.resolve() / "auth"
    r = _helper(f"auth_lock {shlex.quote(str(auth))} owner {STRANGER_UID} ea ownergrp",
                world.root, world.log, world.path)
    assert r.returncode == 3 and "install -d -o 'owner' -g ea -m 0750" in r.stderr
    assert world.calls() == [] and not list(auth.glob(".quarantine-*"))


def test_a_directory_that_gains_a_foreign_entry_is_quarantined_whole(world):
    """The tree check runs again after the modes are tightened: a directory
    group ea could write may gain an entry between the two."""
    world.tree(world.ea)
    auth = world.ea.resolve() / "auth"
    count = shlex.quote(str(world.root / "probes"))
    stand_in = (f'tc_foreign_in() {{ echo x >> {count}; '
                f'[ "$(wc -l < {count})" -ge 2 ] && echo "$1/new"; true; }}\n')
    r = _helper(stand_in + f"auth_lock {shlex.quote(str(auth))} owner {ME} ea ownergrp",
                world.root, world.log, world.path)
    assert r.returncode == 0, r.stderr
    assert (_quarantine(auth) / "profile").is_dir() and "gained an entry" in r.stderr
    assert not [a for a in _auth_chowns(world, auth) if a.startswith("-R ")]


def test_the_owners_own_quarantine_dir_stays_as_it_is(world):
    world.tree(world.ea)
    auth = world.ea.resolve() / "auth"
    old = auth / ".quarantine-20260101000000-1"
    old.mkdir(mode=0o755)
    (old / "scheduled-send-hmac.key").write_text("k")
    r = _helper(f"auth_lock {shlex.quote(str(auth))} owner {ME} ea ownergrp",
                world.root, world.log, world.path)
    assert r.returncode == 0, r.stderr
    assert _mode(old) == 0o700 and (old / "scheduled-send-hmac.key").exists()
    assert sorted(auth.glob(".quarantine-*")) == [old], "nothing new was quarantined"
    assert not [a for a in _auth_chowns(world, auth) if old.name in a]


# ── the mediation enabler ─────────────────────────────────────────────

ENABLER = (REPO / "deploy" / "enable-mail-mediation.sh").read_text()


def _enabler_fn(*names: str) -> str:
    out = []
    for n in names:
        i = ENABLER.index(f"\n{n}(){{") + 1
        j = ENABLER.index("\n}\n", i) + 3
        out.append(ENABLER[i:j])
    return "".join(out)


def _lock_world(world: World, owner_uid: int = ME, prelude: str = "") -> subprocess.CompletedProcess:
    if not world.ea.exists():
        world.tree(world.ea)
    body = ("set -uo pipefail\n"
            f"REAL_USER=owner REAL_UID={owner_uid} REAL_GROUP=ownergrp DARWIN=0 "
            "PERMREC_NAME=.pre-mediation-perms\n"
            f"AUTH={shlex.quote(str(world.ea / 'auth'))}\n"
            f"AUTH_PHYS={shlex.quote(str(world.ea.resolve() / 'auth'))}\n"
            "red(){ echo \"$*\" >&2; }; grn(){ echo \"$*\"; }\n"
            + _enabler_fn("perm_line", "tokens_here", "lock_tokens_here", "lock_tokens")
            + prelude + "lock_tokens\n")
    return _helper(body, world.root, world.log, world.path)


def test_the_enabler_locks_tokens_owner_only_and_records_them_once(world):
    r = _lock_world(world)
    assert r.returncode == 0, r.stderr
    auth = world.ea.resolve() / "auth"
    assert _mode(auth) == 0o750
    for name in ("msal_token_cache.json", "google_token_a_at_b_c.json", "google_client_secret.json"):
        assert _mode(auth / name) == 0o600, name
    rec = (auth / ".pre-mediation-perms").read_text()
    assert "./msal_token_cache.json" in rec and _mode(auth / ".pre-mediation-perms") == 0o600
    assert not [c for c in world.calls("chown") if "ea-bot" in c[3]]
    assert all(c[2] == str(auth) or c[2].startswith(str(auth) + "/") for c in world.calls("chown"))


def test_no_path_in_the_enabler_gives_a_token_to_ea_bot():
    assert "restore_tokens" not in ENABLER
    assert "chown ea-bot:ea \"$f\"" not in ENABLER and "0640" not in ENABLER
    # The tokens are locked before the service, the flag or any check.
    enable = ENABLER[ENABLER.index("\nenable(){"):]
    assert enable.index("lock_tokens ||") < enable.index("install_unit") < enable.index("set_flag true")
    rollback = ENABLER[ENABLER.index("\nrollback(){"):ENABLER.index("\nenable(){")]
    assert "lock_tokens" in rollback and "chown ea-bot" not in rollback
    # The queue is made as ea-bot, never chowned by root.
    assert "chown ea-bot:ea \"$QUEUE\"" not in ENABLER
    assert "sudo -u ea-bot sh -c 'mkdir -p \"$1\" && chgrp ea \"$1\" && chmod 2770 \"$1\"'" in ENABLER


def test_the_enabler_stops_at_an_auth_dir_the_owner_does_not_own(world):
    r = _lock_world(world, owner_uid=STRANGER_UID)
    assert r.returncode != 0 and "Never chown a file from the old directory" in r.stderr
    auth = world.ea.resolve() / "auth"
    assert not (auth / ".pre-mediation-perms").exists(), "no record written before the check"
    assert world.calls("chown") == [] and world.calls("chmod") == []


def test_the_enabler_quarantines_a_token_the_owner_does_not_own(world):
    world.tree(world.ea)
    r = _lock_world(world, prelude=_foreign("msal_token_cache.json"))
    assert r.returncode == 0, r.stderr
    auth = world.ea.resolve() / "auth"
    assert (_quarantine(auth) / "msal_token_cache.json").exists()
    assert not (auth / "msal_token_cache.json").exists()
    assert not [a for a in _auth_chowns(world, auth) if a.endswith("./msal_token_cache.json")]
    assert _mode(auth / "google_token_a_at_b_c.json") == 0o600


def test_the_enabler_takes_the_directory_only_through_the_checked_lock():
    body = _enabler_fn("lock_tokens_here")
    assert 'chown "$REAL_USER:ea" .' not in ENABLER
    assert (body.index("auth_dir_lock_here") < body.index('"$PERMREC_NAME" ]')
            < body.index("auth_entries_lock_here"))
    assert 'REAL_UID="$(id -u "$REAL_USER")"' in ENABLER


def test_changed_scripts_parse():
    for rel in SCRIPTS + ["deploy/enable-mail-mediation.sh", "deploy/tree-custody.sh",
                          "deploy/enable-resident-netpolicy.sh"]:
        r = subprocess.run(["bash", "-n", str(REPO / rel)], capture_output=True, text=True)
        assert r.returncode == 0, (rel, r.stderr)
