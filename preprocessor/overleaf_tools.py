#!/usr/bin/env python3
"""overleaf_tools: the EA's git work on Overleaf projects.

The EA has an Overleaf account of its own. The owner invites that account to a
project (Editor to write, Viewer to read), and a token minted on it
(~/.config/overleaf/token, installed by deploy/install-overleaf-token.sh)
reaches those projects and no other: the collaborator list in Overleaf is
the security boundary. ea-bot holds one clone per project. The lane allows
no raw git, because git's own options run arbitrary commands; every step is
a verb here, and the one verb that sends work to Overleaf waits for the
owner's click.

    overleaf_tools.py list                              the clones ea-bot holds
    overleaf_tools.py clone   --project P --dir D       clone a project the EA is invited to
    overleaf_tools.py status  --dir D                   local changes, and Overleaf commits the clone lacks
    overleaf_tools.py diff    --dir D [--overleaf]      what is unpushed, or what Overleaf has that the clone lacks
    overleaf_tools.py commit  --dir D --message M       commit every change as the bot account; nothing leaves the host
    overleaf_tools.py pull    --dir D [--prefer local|overleaf]
    overleaf_tools.py preview --dir D --commit SHA      what pushing that commit sends (read only)
    overleaf_tools.py push    --dir D --project P --commit SHA --base SHA   gated: the owner's click

pull merges Overleaf's newer commits into the clone. When both sides
changed the same lines it stops, leaves the clone as it was and names the
files, unless --prefer says which side keeps those lines; every other
change from both sides is kept. commit refuses an em-dash on a line the EA
added.

push is gated (publish_gate.py). The EA stages the commit in
~/.ea/state/pending-publish.json; the bot runs preview, posts a card, and on
the owner's click runs push with a one-time token (EA_CARD_TOKEN, or
--confirm-token) bound to the commit and to the Overleaf head the card
showed (--base). push refuses without that token, when the clone names
another project than --project (the token binds the project id too), when
Overleaf's branch is no longer --base, and when a line the commit adds holds an em-dash; then it
pushes exactly that commit, never a force.

Every clone lives under one of ROOTS. git runs through safe_git.py: the
system and global config off, hooks, fsmonitor, pagers and diff programs
overridden, attributes read from the empty tree, the credential helper on
the command line, and a clone whose own .git/config holds anything outside
SAFE_KEYS is refused, so nothing written into a clone or into ~/.gitconfig
runs. Fetch and push go to https://git.overleaf.com/<project>, built from
the validated project id, never to whatever the clone's remote says, and
the owner's token binds that project id.

Output is one JSON object on stdout. A refusal is {"status": "error", ...}
on stderr with exit 2.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    import publish_gate as _gate
    import safe_git as _sg
except ModuleNotFoundError:  # imported as preprocessor.overleaf_tools
    from preprocessor import publish_gate as _gate
    from preprocessor import safe_git as _sg

OVERLEAF_GIT = "https://git.overleaf.com"
PROJECT_URL = "https://www.overleaf.com/project/"
EA_DIR = Path(os.environ.get("EA_DIR") or (Path.home() / ".ea")).resolve()
GITHUB_DEFAULTS = {"bot_login": "", "bot_email": "", "bot_token_dir": "",
                   "doc_review": {"repo": "", "owner_login": ""}, "sites": {}}


def _github_config() -> dict:
    """<EA_DIR>/config/github.json with every key present (the template is
    config/github.json); `_`-prefixed keys are documentation and dropped."""
    try:
        raw = json.loads((EA_DIR / "config" / "github.json").read_text())
    except (OSError, ValueError):
        raw = {}
    cfg = {k: v for k, v in raw.items() if not str(k).startswith("_")} if isinstance(raw, dict) else {}
    out = {**GITHUB_DEFAULTS, **cfg}
    out["doc_review"] = {**GITHUB_DEFAULTS["doc_review"],
                         **{k: v for k, v in (cfg.get("doc_review") or {}).items() if not str(k).startswith("_")}}
    out["sites"] = {k: v for k, v in (cfg.get("sites") or {}).items() if not str(k).startswith("_")}
    return out


_GH = _github_config()
# The commit identity (git user.name / user.email): github.json bot_login and bot_email.
BOT_NAME: str = str(_GH["bot_login"] or "")
BOT_EMAIL: str = str(_GH["bot_email"] or "")
PROJECT_RE = re.compile(r"^[0-9a-f]{24}$")
PROJECT_IN_URL_RE = re.compile(
    r"^https://(?:www\.overleaf\.com/(?:project|read)/|(?:git@)?git\.overleaf\.com/)"
    r"([0-9a-f]{24})/?$")
REMOTE_RE = re.compile(r"^https://(?:git@)?git\.overleaf\.com/([0-9a-f]{24})/?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_KEYS = _sg.SAFE_KEYS
TEXT_SUFFIXES = {".tex", ".bib", ".md", ".txt"}
LATEX_SUFFIXES = {".tex", ".bib"}
LATEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$")
LATEX_EM_DASH_RE = re.compile(r"(?<!-)---(?!-)")
LIST_DEPTH = 4
GIT_TIMEOUT_S = 180


class OverleafError(Exception):
    """A refusal or a git failure, reported as the tool's error shape."""


def _home() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def roots() -> list[Path]:
    """Where clones may live: the EA's documents, shared research, letters,
    proposals, and state (the CV clone)."""
    h = _home()
    return [h / ".ea" / "docs", h / ".ea" / "research", h / ".ea" / "letters",
            h / ".ea" / "state", h / "proposals", h / "letters-of-recommendation"]


def token_file() -> Path:
    return _home() / ".config" / "overleaf" / "token"


def remote_url(project: str) -> str:
    return f"{OVERLEAF_GIT}/{project}"


def project_url(project: str) -> str:
    return PROJECT_URL + project


def parse_project(text: str) -> str:
    """A project id from the id itself, the editor URL or the git URL."""
    text = (text or "").strip()
    if PROJECT_RE.match(text):
        return text
    m = PROJECT_IN_URL_RE.match(text)
    if m:
        return m.group(1)
    raise OverleafError(f"not an Overleaf project id or URL: {text!r} (the id is the "
                        f"24 hex digits after /project/ in the editor URL)")


# --- paths and the clone's own config ----------------------------------------

def _inside_roots(path: Path) -> bool:
    for root in roots():
        r = root.resolve()
        if path == r or r in path.parents:
            return True
    return False


def _resolve(d: str) -> Path:
    path = Path(os.path.expanduser(d or ""))
    if not path.is_absolute():
        raise OverleafError(f"--dir must be an absolute path, not {d!r}")
    path = path.resolve()
    if not _inside_roots(path):
        raise OverleafError(f"{path} is outside the clone roots: "
                            + ", ".join(str(r) for r in roots()))
    return path


def _config_entries(clone: Path) -> list[tuple[str, str]]:
    try:
        return _sg.config_entries(clone)
    except _sg.GitError as e:
        raise OverleafError(str(e)) from None


def _audit(clone: Path) -> str:
    """The clone's project id, after checking its .git holds nothing that
    runs code or redirects git (safe_git.audit)."""
    try:
        entries = _sg.audit(clone, SAFE_KEYS)
    except _sg.GitError as e:
        raise OverleafError(str(e)) from None
    project = None
    for key, value in entries:
        if key == "remote.origin.url":
            m = REMOTE_RE.match(value)
            if not m:
                raise OverleafError(f"{clone} is not an Overleaf clone (origin is not "
                                    f"{OVERLEAF_GIT}/<project>)")
            project = m.group(1)
    if not project:
        raise OverleafError(f"{clone} has no Overleaf origin")
    return project


def _open(d: str) -> tuple[Path, str, str]:
    clone = _resolve(d)
    project = _audit(clone)
    branch = _git(clone, "symbolic-ref", "--short", "HEAD").strip()
    if not BRANCH_RE.match(branch):
        raise OverleafError(f"unexpected branch {branch!r} in {clone}")
    return clone, project, branch


# --- git ---------------------------------------------------------------------

def _credential_helper() -> str:
    return _sg.credential_helper(token_file(), "git")


def _bot_identity() -> tuple[str, str]:
    if not BOT_NAME or not BOT_EMAIL:
        raise OverleafError("github.json names no bot_login or bot_email; see docs/SETUP.md")
    return BOT_NAME, BOT_EMAIL


def _explain(err: str) -> str:
    """Overleaf's git answers, in words the lane can act on."""
    if "authentication token" in err:
        return ("Overleaf did not accept the EA's token (revoked, expired or mistyped). "
                "The owner mints a new one on the EA's Overleaf account and runs "
                "deploy/install-overleaf-token.sh")
    if "Authentication failed" in err:
        return ("the EA's Overleaf account is not a member of this project. The owner invites "
                "it in Overleaf (Share: Editor to write, Viewer to read)")
    return err.strip()[:400]


def _git(clone: Path, *args: str) -> str:
    """git in an audited clone (safe_git.run)."""
    try:
        r = _sg.run(clone, *args, identity=_bot_identity(),
                    credentials={OVERLEAF_GIT: _credential_helper()},
                    timeout=GIT_TIMEOUT_S, safe_keys=SAFE_KEYS)
    except _sg.GitError as e:
        raise OverleafError(str(e)) from None
    if r.returncode != 0:
        raise OverleafError(f"git {args[0]}: {_explain(r.stderr or r.stdout)}")
    return r.stdout


def _need_token() -> None:
    if not token_file().is_file():
        raise OverleafError(f"no Overleaf token at {token_file()}; the owner installs it "
                            f"with deploy/install-overleaf-token.sh")


def _fetch(clone: Path, project: str, branch: str) -> None:
    _need_token()
    _git(clone, "fetch", "-q", "--no-tags", remote_url(project),
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


def _ahead_behind(clone: Path, branch: str) -> tuple[int, int]:
    out = _git(clone, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}").split()
    return int(out[0]), int(out[1])


def _head(clone: Path) -> str:
    return _git(clone, "rev-parse", "--short", "HEAD").strip()


def _changes(clone: Path) -> list[dict[str, str]]:
    """Uncommitted changes in the clone, untracked files included."""
    out = _git(clone, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the rename's source path follows
        items.append({"path": path, "change": _describe(code)})
    return items


def _describe(code: str) -> str:
    if code == "??" or "A" in code:
        return "added"
    if "D" in code:
        return "deleted"
    if "R" in code:
        return "renamed"
    return "modified"


def _em_dash_lines(clone: Path, base: str, cached: bool, sha: str = "HEAD") -> list[str]:
    """Lines added since `base` (staged, or committed up to `sha` when not
    `cached`) that hold an em-dash: the character, or a LaTeX `---`
    outside a comment."""
    args = ["diff", *_sg.REVIEW_DIFF_FLAGS, "-U0"]
    args += ["--cached", base] if cached else [base, sha]
    hits, path, line_no = [], "", 0
    for line in _git(clone, *args).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            suffix = Path(path).suffix.lower()
            text = line[1:]
            if suffix in TEXT_SUFFIXES and (
                    "—" in text
                    or (suffix in LATEX_SUFFIXES
                        and LATEX_EM_DASH_RE.search(LATEX_COMMENT_RE.sub("", text)))):
                hits.append(f"{path}:{line_no}")
            line_no += 1
    return hits


def _refuse_dashes(hits: list[str]) -> None:
    if hits:
        raise OverleafError("em-dash on an added line in " + ", ".join(hits[:20]) +
                            "; rewrite with a comma, colon or parentheses")


def _merge(clone: Path, branch: str, ahead: int, prefer: str | None) -> None:
    """Bring origin/<branch> into HEAD. A conflict leaves the clone as it
    was and names the files, unless `prefer` picks the side that keeps the
    conflicting lines."""
    if not ahead:
        _git(clone, "merge", "-q", "--ff-only", f"origin/{branch}")
        return
    args = ["merge", "-q", "--no-edit", "-m", "Merge Overleaf edits"]
    if prefer:
        args += ["-X", "ours" if prefer == "local" else "theirs"]
    try:
        _git(clone, *args, f"origin/{branch}")
    except OverleafError as e:
        conflicted = [p for p in _git(clone, "diff", "--name-only", "--diff-filter=U")
                      .splitlines() if p]
        try:
            _git(clone, "merge", "--abort")
        except OverleafError:
            pass
        if conflicted and prefer:
            raise OverleafError(
                "git could not combine " + ", ".join(conflicted) + " even with --prefer "
                "(a file deleted on one side and changed on the other). Nothing was "
                "merged; ask the owner") from e
        if conflicted:
            raise OverleafError(
                "Overleaf and the clone changed the same lines in " + ", ".join(conflicted) +
                ". Nothing was merged. Run diff --overleaf to see Overleaf's change, ask "
                "the owner which side keeps those lines, then pull --prefer local or "
                "--prefer overleaf") from e
        raise


@contextmanager
def _lock(clone: Path):
    """One writer at a time per clone: two lanes may act on one project."""
    with open(clone / ".git" / "overleaf_tools.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def _base(clone: Path, branch: str) -> str:
    return _git(clone, "merge-base", "HEAD", f"origin/{branch}").strip()


def _full_head(clone: Path) -> str:
    return _git(clone, "rev-parse", "HEAD").strip()


def _staging_hint(clone: Path, sha: str) -> dict:
    """The push request the EA stages for this commit."""
    return {"file": f"~/.ea/state/{_gate.STAGE_FILE}",
            "request": {"overleaf": str(clone), "commit": sha}}


def _pushable(clone: Path, project: str, branch: str, commit: str) -> tuple[str, str]:
    """(commit, base): the full commit and Overleaf's branch head, after a
    fetch, once the commit is a fast-forward of that head that adds no
    em-dash."""
    if not _gate.SHA_RE.match(commit or ""):
        raise OverleafError(f"--commit must be a commit id (7 to 40 hex digits), not {commit!r}")
    _fetch(clone, project, branch)
    try:
        sha = _git(clone, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").strip()
    except OverleafError:
        raise OverleafError(f"{commit} is not a commit in {clone}") from None
    base = _git(clone, "rev-parse", f"origin/{branch}").strip()
    if sha == base:
        raise OverleafError(f"{sha[:7]} is what Overleaf already has; nothing to push")
    try:
        _git(clone, "merge-base", "--is-ancestor", base, sha)
    except OverleafError:
        raise OverleafError(f"{sha[:7]} does not build on Overleaf's {branch} ({base[:7]}): "
                            f"the project changed. Run pull, then stage the new head") from None
    _refuse_dashes(_em_dash_lines(clone, base, cached=False, sha=sha))
    return sha, base


# --- verbs -------------------------------------------------------------------

def cmd_list() -> dict:
    clones = []
    for root in roots():
        if not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root):
            here = Path(dirpath)
            if ".git" in dirnames:
                dirnames[:] = []
                try:
                    origin = dict(_config_entries(here)).get("remote.origin.url", "")
                    if not REMOTE_RE.match(origin):
                        continue
                    project = _audit(here)
                except OverleafError as e:
                    clones.append({"dir": str(here), "problem": str(e)})
                    continue
                clones.append({"dir": str(here), "project": project,
                               "project_url": project_url(project)})
                continue
            depth = len(here.relative_to(root).parts)
            dirnames[:] = [] if depth >= LIST_DEPTH else [
                n for n in dirnames if not n.startswith(".") and n != "node_modules"]
    return {"status": "ok", "clones": sorted(clones, key=lambda c: c["dir"])}


def cmd_clone(project_text: str, d: str) -> dict:
    project = parse_project(project_text)
    dest = _resolve(d)
    if dest.exists():
        raise OverleafError(f"{dest} already exists; pick a new directory")
    _need_token()
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _sg.run_outside("clone", "-q", "--no-checkout", remote_url(project), str(dest),
                        identity=_bot_identity(),
                        credentials={OVERLEAF_GIT: _credential_helper()},
                        timeout=GIT_TIMEOUT_S)
    if r.returncode != 0:
        raise OverleafError(f"git clone: {_explain(r.stderr or r.stdout)}")
    _git(dest, "config", "remote.origin.url", f"{OVERLEAF_GIT}/{project}")
    # The checkout runs once the new clone passes the audit, with
    # attributes from the empty tree: no filter the project names runs.
    _git(dest, "reset", "-q", "--hard", "HEAD")
    branch = _git(dest, "symbolic-ref", "--short", "HEAD").strip()
    files = [p for p in _git(dest, "ls-files").splitlines() if p]
    return {"status": "cloned", "dir": str(dest), "project": project,
            "project_url": project_url(project), "branch": branch, "head": _head(dest),
            "files": len(files)}


def cmd_status(d: str) -> dict:
    clone, project, branch = _open(d)
    _fetch(clone, project, branch)
    ahead, behind = _ahead_behind(clone, branch)
    return {"status": "ok", "dir": str(clone), "project": project,
            "project_url": project_url(project), "branch": branch, "head": _head(clone),
            "unpushed_commits": ahead, "overleaf_commits_missing": behind,
            "changes": _changes(clone)}


def cmd_diff(d: str, overleaf: bool, max_chars: int) -> dict:
    clone, project, branch = _open(d)
    if overleaf:
        _fetch(clone, project, branch)
        diff = _git(clone, "diff", *_sg.REVIEW_DIFF_FLAGS, f"HEAD...origin/{branch}")
        untracked = []
    else:
        diff = _git(clone, "diff", *_sg.REVIEW_DIFF_FLAGS, _base(clone, branch))
        untracked = [p for p in _git(clone, "ls-files", "-z", "--others",
                                     "--exclude-standard").split("\0") if p]
    return {"status": "ok", "dir": str(clone), "side": "overleaf" if overleaf else "local",
            "diff": diff[:max_chars], "truncated": len(diff) > max_chars,
            "untracked": untracked}


def cmd_commit(d: str, message: str) -> dict:
    message = (message or "").strip()
    if not message:
        raise OverleafError("a commit needs --message: one line saying what changed")
    clone, project, _ = _open(d)
    with _lock(clone):
        if not _changes(clone):
            return {"status": "unchanged", "dir": str(clone), "head": _head(clone),
                    "commit": _full_head(clone)}
        _git(clone, "add", "-A")
        _refuse_dashes(_em_dash_lines(clone, "HEAD", cached=True))
        _git(clone, "commit", "-q", "-m", message)
        files = [p for p in _git(clone, "diff-tree", "--root", "--no-commit-id", "--name-only",
                                 "-r", "HEAD").splitlines() if p]
        sha = _full_head(clone)
        return {"status": "committed", "dir": str(clone), "project": project,
                "head": _head(clone), "commit": sha, "files": files,
                "stage": _staging_hint(clone, sha)}


def cmd_pull(d: str, prefer: str | None) -> dict:
    clone, project, branch = _open(d)
    with _lock(clone):
        _fetch(clone, project, branch)
        changes = _changes(clone)
        if changes:
            raise OverleafError(f"the clone has {len(changes)} uncommitted change(s); "
                                f"commit them first (commit --message ...), then pull")
        ahead, behind = _ahead_behind(clone, branch)
        if not behind:
            return {"status": "unchanged", "dir": str(clone), "head": _head(clone)}
        _merge(clone, branch, ahead, prefer)
        res = {"status": "pulled", "dir": str(clone), "overleaf_commits": behind,
               "merged_with_local_commits": bool(ahead), "head": _head(clone),
               "commit": _full_head(clone)}
        if ahead:
            res["stage"] = _staging_hint(clone, res["commit"])
        return res


def cmd_preview(d: str, commit: str, max_chars: int) -> dict:
    """What pushing `commit` sends: the files and the diff against
    Overleaf's branch, for the owner's card. Read only."""
    clone, project, branch = _open(d)
    with _lock(clone):
        sha, base = _pushable(clone, project, branch, commit)
        names = _git(clone, "diff", "--name-status", "--no-renames", base, sha)
        diff = _git(clone, "diff", *_sg.REVIEW_DIFF_FLAGS, base, sha)
        subject = _git(clone, "log", "-1", "--format=%s", sha).strip()
        count = int(_git(clone, "rev-list", "--count", f"{base}..{sha}").strip() or 0)
    kinds = {"A": "added", "D": "deleted", "M": "modified", "T": "modified"}
    files = []
    for line in names.splitlines():
        code, _, path = line.partition("\t")
        if path:
            files.append({"path": path, "change": kinds.get(code[:1], "modified")})
    return {"status": "ok", "dir": str(clone), "project": project,
            "project_url": project_url(project), "branch": branch, "commit": sha,
            "base": base, "subject": subject, "commits": count, "files": files,
            "diff": diff[:max_chars], "truncated": len(diff) > max_chars}


def cmd_push(d: str, commit: str, base: str, token: str | None = None,
             project: str | None = None) -> dict:
    """Push exactly `commit` to Overleaf, on the owner's one-time token for
    that commit on top of `base` in that project (publish_gate). The token
    binds the project id the clone names, so a clone whose origin moved to
    another project after the card is refused."""
    if not (_gate.FULL_SHA_RE.match(commit or "") and _gate.FULL_SHA_RE.match(base or "")):
        raise OverleafError("push takes the full --commit and --base the owner's card named")
    if project is not None and not PROJECT_RE.match(project):
        raise OverleafError(f"--project must be the 24 hex digit project id, not {project!r}")
    clone, have, branch = _open(d)
    if project is not None and have != project:
        raise OverleafError(f"{clone} pushes to project {have}, not the {project} the card "
                            f"showed; stage the commit again")
    project = have
    with _lock(clone):
        sha, head = _pushable(clone, project, branch, commit)
        if head != base:
            raise OverleafError(f"Overleaf's {branch} is {head[:7]}, not the {base[:7]} the card "
                                f"showed: the project changed after the card. Run pull, then "
                                f"stage the new head")
        try:
            _gate.require_token(token, {"target": "overleaf", "name": str(clone),
                                        "commit": sha, "base": base, "project": project})
        except _gate.GateError as e:
            raise OverleafError(str(e)) from None
        try:
            _git(clone, "push", "-q", remote_url(project), f"{sha}:refs/heads/{branch}")
        except OverleafError as e:
            raise OverleafError(f"Overleaf refused the push; the project may have changed "
                                f"meanwhile. The commit is kept in the clone: run pull, "
                                f"then stage the new head ({e})") from e
        _git(clone, "update-ref", f"refs/remotes/origin/{branch}", sha)
        files = [p for p in _git(clone, "diff", "--name-only", base, sha).splitlines() if p]
        return {"status": "pushed", "dir": str(clone), "project": project,
                "project_url": project_url(project), "commit": sha, "files": files}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="The EA's git work on Overleaf projects.", allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", allow_abbrev=False)
    c = sub.add_parser("clone", allow_abbrev=False)
    c.add_argument("--project", required=True)
    c.add_argument("--dir", required=True)
    for verb in ("status", "diff", "commit", "pull", "preview", "push"):
        s = sub.add_parser(verb, allow_abbrev=False)
        s.add_argument("--dir", required=True)
        if verb == "diff":
            s.add_argument("--overleaf", action="store_true",
                           help="show what Overleaf has that the clone lacks")
        if verb in ("diff", "preview"):
            s.add_argument("--max-chars", type=int, default=20000)
        if verb == "commit":
            s.add_argument("--message", required=True)
        if verb == "pull":
            s.add_argument("--prefer", choices=("local", "overleaf"),
                           help="which side keeps lines both sides changed")
        if verb in ("preview", "push"):
            s.add_argument("--commit", required=True)
        if verb == "push":
            s.add_argument("--base", required=True,
                           help="Overleaf's branch head as the owner's card showed it")
            s.add_argument("--project", required=True,
                           help="the project id the owner's card showed")
            s.add_argument("--confirm-token", default=None,
                           help=f"the one-time token (default: ${_gate.TOKEN_ENV})")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.verb == "list":
            res = cmd_list()
        elif args.verb == "clone":
            res = cmd_clone(args.project, args.dir)
        elif args.verb == "status":
            res = cmd_status(args.dir)
        elif args.verb == "diff":
            res = cmd_diff(args.dir, args.overleaf, args.max_chars)
        elif args.verb == "commit":
            res = cmd_commit(args.dir, args.message)
        elif args.verb == "pull":
            res = cmd_pull(args.dir, args.prefer)
        elif args.verb == "preview":
            res = cmd_preview(args.dir, args.commit, args.max_chars)
        else:
            res = cmd_push(args.dir, args.commit, args.base, token=args.confirm_token,
                           project=args.project)
    except (OverleafError, OSError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
