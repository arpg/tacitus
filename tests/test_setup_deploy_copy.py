"""The ea-bot setup scripts run git in the lanes' copy only after the check.

scripts/install/setup-ea-user.sh and scripts/install/setup-ea-user-macos.sh run as root and deploy the
source into ea-bot's home, where ea-bot can put its own tree or a symlink
at the copy's name. Step 3 of each script enters the copy and checks the
directory it is in before any git command or chown there (the deploy hook's
deploy_tree_ok + deploy_git, docs/SECURITY.md "Owner trust rules"):

- a symlink at the copy's name, or a .git that is one, is refused, and git
  never runs in the tree behind it;
- a copy that others can write, or that neither root nor the installing
  user owns, is refused;
- git works on the copy alone, whatever GIT_* variables the caller has;
- a fresh copy is cloned into a directory the script has just made, and the
  ownership change is made from inside the checked copy, never by name.

Each test runs Step 3 alone, cut out of the script, in a temp directory with
the script's variables pointed there. A stub `chown` records its calls; for
the macOS script a stub `stat` maps BSD stat's -f formats onto GNU stat.
Nothing runs as root, and no path outside the temp directory is touched.
"""
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh"]
sys.path.insert(0, str(REPO / "scripts"))

import boundary_lint as bl  # noqa: E402

REAL_STAT = shutil.which("stat") or "/usr/bin/stat"

CHOWN_STUB = """#!/usr/bin/env bash
# Stands in for chown: records the directory it ran in and its arguments.
printf '%s|%s\\n' "$(pwd -P)" "$*" >> "$CHOWN_LOG"
"""

BSD_STAT_STUB = f"""#!/usr/bin/env bash
# BSD stat on GNU stat: -f FORMAT, where %u is the owner's uid and %Lp the
# permission bits in octal (GNU %a).
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -f) fmt=$2; [ "$fmt" = %Lp ] && fmt=%a; args+=(-c "$fmt"); shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
exec {shlex.quote(REAL_STAT)} "${{args[@]}}"
"""

MARKER_HOOK = """#!/bin/sh
# Planted by the test: git ran in this tree.
echo "$0" >> "{marker}"
"""


def _git_free_env(home: Path) -> dict:
    """This process's environment without GIT_* variables, with HOME at
    home and an empty global config there, and no system config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / ".gitconfig"
    cfg.touch()
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(*args: str, cwd: Path, home: Path) -> str:
    """git in a fixture repo only: no hooks, identity on the command line."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True,
                          env=_git_free_env(home)).stdout.strip()


def _step3(script: str) -> str:
    text = (REPO / script).read_text()
    start = text.index("# ── Step 3: Deploy source code to ea-bot's home")
    return text[start:text.index("# ── Step 4:", start)]


class World:
    """A source repo, ea-bot's home and the stubs, all under one temp dir."""

    def __init__(self, root: Path, script: str):
        self.root = root.resolve()
        self.script = script
        self.gh = self.root / "git-home"
        self.src = self.root / "src"
        self.home = self.root / "ea-bot-home"
        self.copy = self.home / "ea-system"
        self.log = self.root / "chown.log"
        self.marker = self.root / "planted-hook-ran"
        self.home.mkdir(mode=0o755)
        self.src.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.src)
        self.commit(self.src, "a.txt", "v1\n")
        stubs = self.root / "stubs"
        stubs.mkdir()
        (stubs / "chown").write_text(CHOWN_STUB)
        (stubs / "chown").chmod(0o755)
        if script.endswith("-macos.sh"):
            if sys.platform != "darwin":
                # On Linux the macOS script needs BSD stat's -f mapped onto GNU
                # stat; a real Mac has BSD stat and the stub would call it with -c.
                (stubs / "stat").write_text(BSD_STAT_STUB)
                (stubs / "stat").chmod(0o755)
        self.path = f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def git(self, *args: str, cwd: Path) -> str:
        return _git(*args, cwd=cwd, home=self.gh)

    def commit(self, repo: Path, name: str, text: str) -> str:
        (repo / name).write_text(text)
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-qm", name, cwd=repo)
        return self.git("rev-parse", "HEAD", cwd=repo)

    def head(self, repo: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=repo)

    def clone(self, dest: Path) -> Path:
        """A copy as an earlier setup left it: a clone, 755 all the way."""
        self.git("clone", "-q", str(self.src), str(dest), cwd=self.root)
        for d in (dest, dest / ".git"):
            os.chmod(d, 0o755)
        return dest

    def planted_tree(self) -> Path:
        """ea-bot's own repo, with a hook that records any git run in it."""
        evil = self.root / "planted"
        evil.mkdir()
        self.git("init", "-q", "-b", "main", cwd=evil)
        self.commit(evil, "planted.txt", "ea-bot's tree\n")
        for name in ("reference-transaction", "post-checkout", "post-merge"):
            hook = evil / ".git" / "hooks" / name
            hook.write_text(MARKER_HOOK.format(marker=self.marker))
            hook.chmod(0o755)
        for d in (evil, evil / ".git"):
            os.chmod(d, 0o755)
        return evil

    def run(self, real_uid: int | None = None, extra_env: dict | None = None):
        uid = os.getuid() if real_uid is None else real_uid
        assigns = {"REAL_USER": "installer", "REAL_UID": str(uid), "SCRIPT_DIR": str(self.src),
                   "EA_BOT_HOME": str(self.home), "EA_BOT_SCRIPT_DIR": str(self.copy)}
        script = ("set -euo pipefail\n"
                  + "".join(f"{k}={shlex.quote(v)}\n" for k, v in assigns.items())
                  + _step3(self.script) + "echo step3-done\n")
        env = dict(_git_free_env(self.gh), PATH=self.path, CHOWN_LOG=str(self.log))
        env.update(extra_env or {})
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=env, cwd=str(self.root), timeout=120)

    def chowns(self) -> list:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture(params=SCRIPTS)
def world(request, tmp_path):
    return World(tmp_path, request.param)


def _refused(r) -> bool:
    return r.returncode != 0 and "step3-done" not in r.stdout


def test_fresh_install_clones_into_a_directory_it_made(world):
    r = world.run()
    assert r.returncode == 0 and "step3-done" in r.stdout, r.stdout + r.stderr
    assert world.head(world.copy) == world.head(world.src)
    assert world.chowns() == [f"{world.copy}|-R installer:ea ."], world.chowns()
    for d in (world.copy, world.copy / ".git"):
        assert stat.S_IMODE(d.stat().st_mode) & 0o022 == 0, d
    assert stat.S_IMODE((world.copy / "a.txt").stat().st_mode) & 0o022 == 0


def test_update_moves_a_checked_copy_to_the_source_head(world):
    world.clone(world.copy)
    new = world.commit(world.src, "b.txt", "v2\n")
    r = world.run()
    assert r.returncode == 0 and "step3-done" in r.stdout, r.stdout + r.stderr
    assert world.head(world.copy) == new
    assert world.chowns() == [f"{world.copy}|-R installer:ea ."], world.chowns()


def test_git_variables_of_the_caller_never_move_git_off_the_copy(world):
    world.clone(world.copy)
    new = world.commit(world.src, "b.txt", "v2\n")
    other = world.root / "other"
    other.mkdir()
    world.git("init", "-q", "-b", "main", cwd=other)
    other_head = world.commit(other, "o.txt", "other\n")
    r = world.run(extra_env={"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                             "GIT_INDEX_FILE": str(other / ".git" / "index")})
    assert r.returncode == 0 and "step3-done" in r.stdout, r.stdout + r.stderr
    assert world.head(other) == other_head, "git acted on the repository GIT_DIR named"
    assert world.head(world.copy) == new


def test_a_symlink_at_the_copy_name_is_refused(world):
    evil = world.planted_tree()
    evil_head = world.head(evil)
    world.copy.symlink_to(evil)
    r = world.run()
    assert _refused(r), r.stdout + r.stderr
    assert "must be real directories" in r.stderr
    assert world.head(evil) == evil_head, "git ran in the tree behind the symlink"
    assert not world.marker.exists(), "a hook of the planted tree ran"
    assert world.chowns() == [], "the planted tree was handed to the installing user"


def test_a_git_dir_that_is_a_symlink_is_refused(world):
    evil = world.planted_tree()
    evil_head = world.head(evil)
    world.copy.mkdir(mode=0o755)
    (world.copy / ".git").symlink_to(evil / ".git")
    r = world.run()
    assert _refused(r), r.stdout + r.stderr
    assert world.head(evil) == evil_head
    assert not world.marker.exists()
    assert world.chowns() == []


@pytest.mark.parametrize("which,mode", [(".", 0o775), (".", 0o757), (".git", 0o775)])
def test_a_copy_others_can_write_is_refused(world, which, mode):
    world.clone(world.copy)
    old = world.head(world.copy)
    world.commit(world.src, "b.txt", "v2\n")
    os.chmod(world.copy / which, mode)
    r = world.run()
    assert _refused(r), r.stdout + r.stderr
    assert world.head(world.copy) == old
    assert world.chowns() == []


def test_a_copy_owned_by_another_user_is_refused(world):
    if os.getuid() == 0:
        pytest.skip("root owns every fixture, and root-owned copies are accepted")
    world.clone(world.copy)
    old = world.head(world.copy)
    world.commit(world.src, "b.txt", "v2\n")
    r = world.run(real_uid=os.getuid() + 1)
    assert _refused(r), r.stdout + r.stderr
    assert world.head(world.copy) == old
    assert world.chowns() == []


def test_a_name_without_a_git_dir_is_not_cloned_into(world):
    world.copy.mkdir(mode=0o755)
    (world.copy / "left.txt").write_text("left over\n")
    r = world.run()
    assert _refused(r), r.stdout + r.stderr
    assert sorted(p.name for p in world.copy.iterdir()) == ["left.txt"]
    assert world.chowns() == []


def test_the_clone_refuses_a_directory_swapped_in_after_mkdir(world):
    """deploy_git clone checks the directory it enters, so a symlink put at
    the name after the script's mkdir is never cloned into."""
    text = (REPO / world.script).read_text()
    ok = re.search(r"^deploy_tree_ok\(\) \{\n.*?^\}\n", text, re.M | re.S)
    git = re.search(r"^deploy_git\(\) \(\n.*?^\)\n", text, re.M | re.S)
    assert ok and git, "deploy_tree_ok / deploy_git not found"
    target = world.root / "ea-bot-empty"
    target.mkdir(mode=0o755)
    world.copy.symlink_to(target)
    script = (f"REAL_UID={os.getuid()}\nSCRIPT_DIR={shlex.quote(str(world.src))}\n"
              f"EA_BOT_SCRIPT_DIR={shlex.quote(str(world.copy))}\n"
              f"DEPLOY_DIR_PHYS={shlex.quote(str(world.copy))}\n"
              + ok.group(0) + git.group(0) + "deploy_git clone\n")
    env = dict(_git_free_env(world.gh), PATH=world.path)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                       timeout=60)
    assert r.returncode == 97, r.stdout + r.stderr
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_git_command_goes_through_the_checked_helper(script):
    text = (REPO / script).read_text()
    sh_git = [h for h in bl.lint_shell(script, text) if h.rule == "sh-git"]
    assert not sh_git, "\n".join(h.render() for h in sh_git)
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    runs = [ln.strip() for ln in code if re.search(r"(^|[;&|(]|\bexec)\s*git\s", ln)]
    assert runs == ['exec git "$@"'], runs
    assert 'git -C "$EA_BOT_SCRIPT_DIR"' not in text
    assert 'chown -R "$REAL_USER:ea" "$EA_BOT_SCRIPT_DIR"' not in text
    step3 = _step3(script)
    assert step3.index('echo "$UNSAFE_COPY"') < step3.index('deploy_git fetch "$SCRIPT_DIR" HEAD')
    assert text.index('REAL_UID="$(id -u "$REAL_USER")"') < text.index(step3)
    assert subprocess.run(["bash", "-n", str(REPO / script)]).returncode == 0
