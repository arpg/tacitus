#!/usr/bin/env python3
"""site_tools: the EA edits and publishes the static sites it keeps.

A site is a repository served from the root of main: by GitHub Pages
(public), or by a server that pulls main on a timer (private, behind
sign-in; `private: true` on the site). The sites and the bot account come
from ~/.ea/config/github.json (`sites`, `bot_login`, `bot_email`,
`bot_token_dir`; see docs/SETUP.md). ea-bot holds one clone per site at
~/presentations/sites/<slug> and pushes as the bot account with that
account's one GitHub token (~/.config/<bot_token_dir>/token, classic, repo
scope), the same for every site. The token can do what the bot account can
do: it has write access, not admin, to the repositories the owner gave it,
so it cannot change a setting, create or delete a repository or add a
collaborator, and the owner's grants to it are the security boundary. The
lane allows no raw git, because git's own options run arbitrary commands;
every step is a verb here, and the one verb that publishes waits for the
owner's click.

    site_tools.py list
    site_tools.py status   --site S           what changed, and what GitHub has that the clone lacks
    site_tools.py diff     --site S           the uncommitted change as a diff
    site_tools.py pull     --site S           fast-forward the clone to GitHub
    site_tools.py add-file --site S --from PATH --to REL   copy a deck's PDF or an image into the clone
    site_tools.py commit   --site S --message M          commit every change; nothing leaves the host
    site_tools.py revert   --site S --commit SHA         commit the undo of one published commit
    site_tools.py preview  --site S --commit SHA         what publishing that commit sends (read only)
    site_tools.py publish  --site S --commit SHA --base SHA [--no-wait]   gated: the owner's click

commit commits every change in the clone as the bot account. It refuses
when GitHub holds commits the clone lacks, and when a line the change adds
contains an em-dash. revert commits the undo of one published commit.
Neither pushes.

publish is gated (publish_gate.py). The EA stages the commit in
~/.ea/state/pending-publish.json; the bot runs preview, posts a card with
the files and a scan of the added lines, and on the owner's click runs
publish with a one-time token (EA_CARD_TOKEN, or --confirm-token) bound to
the commit and to the GitHub head the card showed (--base). publish
refuses without that token, when GitHub's main is no longer --base, and
when a line the commit adds holds an em-dash; then it pushes exactly that
commit to main (never a force) and waits until the live site serves each
pushed file byte for byte. A site that Jekyll builds in a GitHub Actions
workflow names the workflow in `build`: publish first waits for that
workflow's run on the pushed commit, then compares only the files Jekyll
copies unchanged. A private site is behind sign-in, so publish cannot read
it: it returns live=null and the server's pull timer deploys the push.

git runs through safe_git.py, so nothing the clone's .git or its
.gitattributes says runs or changes what git prints, and fetch and publish
go to the repository github.json names for the site, never to the clone's
remote. The card's diff is every byte as text with the stock prefixes.

add-file exists because the lane has no cp: it copies a PDF or an image
from a deck folder into the clone, and nothing else, so a public site
never receives a file from ~/.ea or a token directory.

Output is one JSON object on stdout. A refusal is {"status": "error", ...}
on stderr with exit 2.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

try:
    import publish_gate as _gate
    import safe_git as _sg
except ModuleNotFoundError:  # imported as preprocessor.site_tools
    from preprocessor import publish_gate as _gate
    from preprocessor import safe_git as _sg

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
# slug -> {title, repo, url, private?, deploy_note?, build?}
SITES: dict[str, dict] = {slug: dict(site) for slug, site in _GH["sites"].items() if isinstance(site, dict)}
BRANCH = "main"
BOT_NAME: str = str(_GH["bot_login"] or "")
BOT_TOKEN_DIR: str = str(_GH["bot_token_dir"] or "")
BOT_EMAIL: str = str(_GH["bot_email"] or "")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
GITHUB_GIT = "https://github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
TEXT_SUFFIXES = {".html", ".htm", ".md", ".markdown", ".yml", ".yaml"}
EM_DASHES = ("—", "&mdash;", "&#8212;", "&#x2014;")
VERIFY_MAX_FILES = 6
WAIT_S = 300
POLL_S = 10
BUILD_POLL_S = 15
# Jekyll leaves these out of every site, on top of the site's own exclude list.
JEKYLL_EXCLUDES = ("Gemfile", "Gemfile.lock", "node_modules", "vendor", "gemfiles")
# add-file: what may go on a public site, and from where.
FILE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
FILE_MAX_BYTES = 50 * 1024 * 1024


class SiteError(Exception):
    """A refusal or a git or network failure, reported as the tool's error shape."""


def _home() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def clone_dir(slug: str) -> Path:
    return _home() / "presentations" / "sites" / slug


def token_file(slug: str) -> Path:
    """The bot account's one token, the same for every site."""
    _site(slug)
    if not BOT_TOKEN_DIR or "/" in BOT_TOKEN_DIR or BOT_TOKEN_DIR in (".", ".."):
        raise SiteError("github.json names no bot_token_dir; see docs/SETUP.md")
    return _home() / ".config" / BOT_TOKEN_DIR / "token"


def file_roots() -> tuple[Path, ...]:
    """The folders add-file copies from: the owner's decks (a read-only
    mount) and the EA's own."""
    return (Path("/srv/ea/presentations/decks"), _home() / "presentations" / "decks")


def _site(slug: str) -> dict:
    if not SITES:
        raise SiteError("github.json names no sites; see docs/SETUP.md")
    try:
        return SITES[slug]
    except KeyError:
        raise SiteError(f"no site {slug!r}; known sites: {', '.join(sorted(SITES))}") from None


def _bot_identity() -> tuple[str, str]:
    if not BOT_NAME or not BOT_EMAIL:
        raise SiteError("github.json names no bot_login or bot_email; see docs/SETUP.md")
    return BOT_NAME, BOT_EMAIL


# --- git (ea-bot's clone) ---------------------------------------------------

def _credential_helper(slug: str) -> str:
    return _sg.credential_helper(token_file(slug), "x-access-token")


def remote_url(slug: str) -> str:
    """Where fetch and publish go: the repository github.json names for the
    site, never the clone's own remote."""
    repo = str(_site(slug).get("repo") or "")
    if not REPO_RE.match(repo) or ".." in repo:
        raise SiteError(f"github.json site {slug} must name repo as owner/name, not {repo!r}")
    return f"{GITHUB_GIT}/{repo}.git"


def _run_git(slug: str, *args: str, text: bool = True):
    clone = clone_dir(slug)
    if not (clone / ".git").is_dir():
        raise SiteError(f"no clone of {_site(slug)['repo']} at {clone}")
    try:
        return _sg.run(clone, *args, identity=_bot_identity(),
                       credentials={GITHUB_GIT: _credential_helper(slug)}, text=text)
    except _sg.GitError as e:
        raise SiteError(str(e)) from None


def _git(slug: str, *args: str) -> str:
    """git in the site's clone, through safe_git.run: no hook, filter,
    pager or diff program the clone names runs, and the credential helper
    comes from this command line."""
    r = _run_git(slug, *args)
    if r.returncode != 0:
        raise SiteError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _fetch(slug: str) -> None:
    _git(slug, "fetch", "-q", remote_url(slug),
         f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")


def _ahead_behind(slug: str) -> tuple[int, int]:
    out = _git(slug, "rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}").split()
    return int(out[0]), int(out[1])


def _changes(slug: str) -> list[dict[str, str]]:
    """Uncommitted changes in the clone, untracked files included."""
    out = _git(slug, "status", "--porcelain=v1", "-z", "--untracked-files=all")
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
    if code == "??":
        return "added"
    if "D" in code:
        return "deleted"
    if "R" in code:
        return "renamed"
    if "A" in code:
        return "added"
    return "modified"


def _added_lines(slug: str, rel: str) -> list[tuple[int, str]]:
    """(line number, text) for each line the uncommitted change adds to a tracked file."""
    out = _git(slug, "diff", *_sg.REVIEW_DIFF_FLAGS, "-U0", "HEAD", "--", rel)
    added, n, in_hunk = [], 0, False
    for line in out.splitlines():
        m = HUNK_RE.match(line)
        if m:
            n, in_hunk = int(m.group(1)), True
        elif in_hunk and line.startswith("+"):
            added.append((n, line[1:]))
            n += 1
    return added


def _em_dash_lines(slug: str, changes: list[dict[str, str]]) -> list[str]:
    """path:line for each added line that holds an em-dash. Only added lines
    count, so an em-dash a co-editor already put in a file does not block
    the EA's edit to another line of it."""
    clone, hits = clone_dir(slug), []
    for c in changes:
        rel, path = c["path"], clone / c["path"]
        if (c["change"] == "deleted" or path.suffix.lower() not in TEXT_SUFFIXES
                or path.is_symlink() or not path.is_file()):
            continue
        if c["change"] == "added":
            lines = list(enumerate(path.read_text(errors="replace").splitlines(), 1))
        else:
            lines = _added_lines(slug, rel)
        hits += [f"{rel}:{n}" for n, text in lines if any(d in text for d in EM_DASHES)]
    return hits


def _range_diff(slug: str, base: str, sha: str, context: int = 3) -> str:
    return _git(slug, "diff", *_sg.REVIEW_DIFF_FLAGS, f"-U{context}", base, sha)


def _range_em_dash_lines(slug: str, base: str, sha: str) -> list[str]:
    """path:line for each line the commits base..sha add that holds an em-dash."""
    return [f"{path}:{n}" for path, n, text in _gate.added_lines(_range_diff(slug, base, sha, 0))
            if Path(path).suffix.lower() in TEXT_SUFFIXES and any(d in text for d in EM_DASHES)]


def _commit_id(slug: str, ref: str) -> str:
    """The full id of a commit in the clone, or a refusal."""
    if not SHA_RE.match(ref or ""):
        raise SiteError(f"--commit must be a commit id (7 to 40 hex digits), not {ref!r}")
    try:
        return _git(slug, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except SiteError:
        raise SiteError(f"{ref} is not a commit in the clone of {slug}") from None


def _is_ancestor(slug: str, older: str, newer: str) -> bool:
    try:
        _git(slug, "merge-base", "--is-ancestor", older, newer)
        return True
    except SiteError:
        return False


def _publishable(slug: str, commit: str) -> tuple[str, str]:
    """(commit, base): the full commit and GitHub's main, after a fetch,
    once the commit is a fast-forward of main that adds no em-dash."""
    _fetch(slug)
    sha = _commit_id(slug, commit)
    base = _git(slug, "rev-parse", f"origin/{BRANCH}").strip()
    if sha == base:
        raise SiteError(f"{sha[:7]} is what GitHub already has; nothing to publish")
    if not _is_ancestor(slug, base, sha):
        raise SiteError(f"{sha[:7]} does not build on GitHub's main ({base[:7]}): someone "
                        f"edited the site, or the commit is old. Run pull, commit again, "
                        f"and stage the new commit")
    dashes = _range_em_dash_lines(slug, base, sha)
    if dashes:
        raise SiteError("em-dash in " + ", ".join(dashes[:20]) +
                        "; rewrite with a comma, colon or parentheses, commit, and stage again")
    return sha, base


@contextmanager
def _lock(slug: str):
    """One publish at a time per clone: two lanes may act on one site."""
    with open(clone_dir(slug) / ".git" / "site_tools.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


# --- the live site ----------------------------------------------------------

def _live_bytes(url: str) -> bytes | None:
    """The file as the site serves it now, or None when it cannot be read.
    The query string makes the CDN fetch past its cached copy."""
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                               "User-Agent": "ea-site-tools"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except (urllib.error.URLError, OSError):
        return None


def _file_url(slug: str, path: str, sha: str) -> str:
    base = _site(slug)["url"]
    return f"{base}{urllib.parse.quote(path)}?v={sha[:12]}"


def _api_json(url: str):
    """A GitHub API read, without a token (the repository is public), or
    None when it cannot be read."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "ea-site-tools"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _build_run(slug: str, sha: str) -> dict | None:
    """The newest run of the site's build workflow on `sha`, or None before
    GitHub starts one."""
    site = _site(slug)
    query = urllib.parse.urlencode({"head_sha": sha, "event": "push", "per_page": 20})
    data = _api_json(f"https://api.github.com/repos/{site['repo']}/actions/runs?{query}")
    runs = [r for r in (data or {}).get("workflow_runs", []) if r.get("path") == site["build"]]
    return runs[0] if runs else None


def _wait_build(slug: str, sha: str, wait_s: int) -> dict | None:
    """Wait for the build of `sha` to finish. Returns the last run seen,
    finished or not, or None when no run started within wait_s."""
    start, run = time.monotonic(), None
    while True:
        run = _build_run(slug, sha) or run
        if (run and run.get("status") == "completed") or time.monotonic() - start >= wait_s:
            return run
        time.sleep(BUILD_POLL_S)


def _git_bytes(slug: str, sha: str, path: str) -> bytes:
    """The committed bytes of `path` at `sha`, as stored: no filter or
    textconv runs."""
    return _run_git(slug, "cat-file", "blob", f"{sha}:{path}", text=False).stdout


def _jekyll_excludes(slug: str, sha: str) -> tuple[str, ...]:
    try:
        import yaml
        config = yaml.safe_load(_git_bytes(slug, sha, "_config.yml")) or {}
        own = tuple(str(p) for p in config.get("exclude") or ())
    except Exception:
        own = ()
    return JEKYLL_EXCLUDES + own


def _served_as_is(path: str, content: bytes, excludes: tuple[str, ...]) -> bool:
    """Whether Jekyll copies this file to the site unchanged. It renders
    Markdown and any file with front matter, and leaves out _ and . paths
    and the excluded ones."""
    parts = path.split("/")
    if any(p.startswith(("_", ".")) for p in parts):
        return False
    if path.lower().endswith((".md", ".markdown")) or content.startswith(b"---"):
        return False
    for pat in excludes:
        pat = pat.rstrip("/")
        if (fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(parts[-1], pat)
                or path.startswith(pat + "/")):
            return False
    return True


def _wait_live(slug: str, sha: str, paths: list[str], wait_s: int) -> tuple[bool, list[str], int]:
    """Wait until every checked file is served as committed at `sha`.
    Returns (live, the files checked, seconds waited)."""
    jekyll = bool(_site(slug).get("build"))
    excludes = _jekyll_excludes(slug, sha) if jekyll else ()
    want: dict[str, bytes] = {}
    for p in paths:
        if p.startswith(".") or len(want) >= VERIFY_MAX_FILES:
            continue
        content = _git_bytes(slug, sha, p)
        if not jekyll or _served_as_is(p, content, excludes):
            want[p] = content
    checked = list(want)
    if not checked:
        return True, [], 0
    start = time.monotonic()
    while True:
        pending = [p for p in checked if _live_bytes(_file_url(slug, p, sha)) != want[p]]
        waited = int(time.monotonic() - start)
        if not pending:
            return True, checked, waited
        if waited >= wait_s:
            return False, checked, waited
        time.sleep(POLL_S)


def _push_and_verify(slug: str, before: str, sha: str, wait: bool) -> dict:
    """Push exactly `sha` to main (never a force) and wait for the live site."""
    site = _site(slug)
    try:
        _git(slug, "push", "-q", remote_url(slug), f"{sha}:refs/heads/{BRANCH}")
    except SiteError as e:
        raise SiteError(f"GitHub refused the push; the site may have changed meanwhile. "
                        f"The commit is kept in the clone: run pull, check the page, "
                        f"commit and stage again ({e})") from e
    _git(slug, "update-ref", f"refs/remotes/origin/{BRANCH}", sha)
    paths = [p for p in _git(slug, "diff", "--name-only", "--diff-filter=AMR",
                             f"{before}..{sha}").splitlines() if p]
    res = {"status": "published", "site": slug, "url": site["url"], "commit": sha[:7],
           "commit_url": f"https://github.com/{site['repo']}/commit/{sha}", "files": paths}
    if site.get("private"):
        res["live"] = None
        res["note"] = site.get("deploy_note", "the site is behind sign-in and deploys on its own timer")
        return res
    if not wait:
        res["live"] = None
        return res
    start = time.monotonic()
    if site.get("build"):
        run = _wait_build(slug, sha, WAIT_S)
        res["build_url"] = run.get("html_url") if run else None
        if not run or run.get("conclusion") != "success":
            res.update(live=False, waited_s=int(time.monotonic() - start))
            if not run:
                res["note"] = (f"GitHub has the commit, but no build of it started within "
                               f"{res['waited_s']} s. Check {site['url']} again in a few minutes.")
            elif run.get("status") != "completed":
                res["note"] = (f"GitHub has the commit and is still building the site. "
                               f"Check {site['url']} again in a few minutes.")
            else:
                res["note"] = (f"the site build failed ({run.get('conclusion')}), so the site "
                               f"still serves the previous version. The log is at build_url; "
                               f"fix the change and publish again.")
            return res
    built_s = int(time.monotonic() - start)
    live, checked, waited = _wait_live(slug, sha, paths, max(WAIT_S - built_s, 0))
    res.update(live=live, checked=checked, waited_s=built_s + waited)
    if not live:
        res["note"] = (f"GitHub has the commit, but the site did not serve it within "
                       f"{built_s + waited} s. Check {site['url']} again in a few minutes.")
    return res


# --- verbs ------------------------------------------------------------------

def cmd_list() -> dict:
    return {"status": "ok", "sites": [
        {"site": slug, "title": s["title"], "url": s["url"], "repo": s["repo"],
         "private": bool(s.get("private")),
         "clone": str(clone_dir(slug)), "cloned": (clone_dir(slug) / ".git").is_dir()}
        for slug, s in sorted(SITES.items())]}


def cmd_status(slug: str) -> dict:
    site = _site(slug)
    _fetch(slug)
    ahead, behind = _ahead_behind(slug)
    return {"status": "ok", "site": slug, "url": site["url"], "repo": site["repo"],
            "clone": str(clone_dir(slug)), "head": _git(slug, "rev-parse", "--short", "HEAD").strip(),
            "unpushed_commits": ahead, "github_commits_missing": behind,
            "changes": _changes(slug)}


def cmd_diff(slug: str, max_chars: int) -> dict:
    diff = _git(slug, "diff", *_sg.REVIEW_DIFF_FLAGS, "HEAD")
    added = [c["path"] for c in _changes(slug) if c["change"] == "added"]
    truncated = len(diff) > max_chars
    return {"status": "ok", "site": slug, "diff": diff[:max_chars], "truncated": truncated,
            "untracked": added}


def cmd_pull(slug: str) -> dict:
    with _lock(slug):
        _fetch(slug)
        ahead, behind = _ahead_behind(slug)
        if not behind:
            return {"status": "unchanged", "site": slug, "head": _git(slug, "rev-parse", "--short", "HEAD").strip()}
        if ahead:
            raise SiteError(f"the clone has {ahead} unpushed commit(s) and GitHub has {behind} "
                            f"newer one(s); ask the owner how to combine them")
        _git(slug, "merge", "-q", "--ff-only", f"origin/{BRANCH}")
        return {"status": "pulled", "site": slug, "commits": behind,
                "head": _git(slug, "rev-parse", "--short", "HEAD").strip()}


def cmd_add_file(slug: str, src: str, dest: str) -> dict:
    """Copy a PDF or an image from a deck folder into the clone at `dest`."""
    try:
        source = Path(src).resolve(strict=True)
    except OSError:
        raise SiteError(f"no file {src}") from None
    roots = [r.resolve() for r in file_roots() if r.exists()]
    if not any(source.is_relative_to(r) for r in roots):
        raise SiteError(f"add-file copies only from the deck folders "
                        f"({', '.join(str(r) for r in file_roots())}), not {source}")
    if not source.is_file() or source.suffix.lower() not in FILE_SUFFIXES:
        raise SiteError(f"add-file copies a PDF or an image ({', '.join(sorted(FILE_SUFFIXES))}), "
                        f"not {source.name}")
    size = source.stat().st_size
    if size > FILE_MAX_BYTES:
        raise SiteError(f"{source.name} is {size // 2**20} MB; the limit is {FILE_MAX_BYTES // 2**20} MB")
    parts = Path(dest).parts
    if (not parts or Path(dest).is_absolute() or ".." in parts
            or any(p.startswith(".") for p in parts)):
        raise SiteError(f"--to must be a path inside the site, such as slides/L14-x.pdf, not {dest!r}")
    if Path(dest).suffix.lower() != source.suffix.lower():
        raise SiteError(f"--to must keep the file type ({source.suffix}), not {dest!r}")
    clone = clone_dir(slug).resolve()
    if not (clone / ".git").is_dir():
        raise SiteError(f"no clone of {_site(slug)['repo']} at {clone}")
    target = clone / dest
    with _lock(slug):
        anchor = target.parent
        while not anchor.exists():
            anchor = anchor.parent
        if target.is_symlink() or not anchor.resolve().is_relative_to(clone):
            raise SiteError(f"{dest} leads outside the clone")
        target.parent.mkdir(parents=True, exist_ok=True)
        replaced = target.exists()
        shutil.copyfile(source, target)
    return {"status": "replaced" if replaced else "added", "site": slug, "path": dest,
            "from": str(source), "bytes": size}


def _staging_hint(slug: str, sha: str) -> dict:
    """The publish request the EA stages for this commit."""
    return {"file": f"~/.ea/state/{_gate.STAGE_FILE}", "request": {"site": slug, "commit": sha}}


def cmd_commit(slug: str, message: str) -> dict:
    """Commit every change in the clone as the bot account. Nothing leaves
    the host; publishing the commit is the owner's click."""
    message = (message or "").strip()
    if not message:
        raise SiteError("a commit needs --message: one line saying what changed")
    with _lock(slug):
        _fetch(slug)
        ahead, behind = _ahead_behind(slug)
        if behind:
            raise SiteError(f"GitHub has {behind} commit(s) the clone lacks (someone edited the "
                            f"site). Run pull, check the page, then commit again")
        changes = _changes(slug)
        if not changes:
            head = _git(slug, "rev-parse", "HEAD").strip()
            res = {"status": "unchanged", "site": slug, "commit": head, "unpushed_commits": ahead}
            if ahead:
                res["stage"] = _staging_hint(slug, head)
            return res
        dashes = _em_dash_lines(slug, changes)
        if dashes:
            raise SiteError("em-dash in " + ", ".join(dashes[:20]) +
                            "; rewrite with a comma, colon or parentheses")
        _git(slug, "add", "-A")
        _git(slug, "commit", "-q", "-m", message)
        sha = _git(slug, "rev-parse", "HEAD").strip()
        return {"status": "committed", "site": slug, "commit": sha,
                "files": [c["path"] for c in changes], "stage": _staging_hint(slug, sha)}


def cmd_preview(slug: str, commit: str, max_chars: int) -> dict:
    """What publishing `commit` sends: the files and the diff against
    GitHub's main, for the owner's card. Read only."""
    site = _site(slug)
    with _lock(slug):
        sha, base = _publishable(slug, commit)
        names = _git(slug, "diff", "--name-status", "--no-renames", base, sha)
        diff = _range_diff(slug, base, sha)
        subject = _git(slug, "log", "-1", "--format=%s", sha).strip()
        count = int(_git(slug, "rev-list", "--count", f"{base}..{sha}").strip() or 0)
    kinds = {"A": "added", "D": "deleted", "M": "modified", "T": "modified"}
    files = []
    for line in names.splitlines():
        code, _, path = line.partition("\t")
        if path:
            files.append({"path": path, "change": kinds.get(code[:1], "modified")})
    return {"status": "ok", "site": slug, "title": site["title"], "url": site["url"],
            "repo": site["repo"],
            "private": bool(site.get("private")), "commit": sha, "base": base,
            "subject": subject, "commits": count, "files": files,
            "diff": diff[:max_chars], "truncated": len(diff) > max_chars}


def cmd_publish(slug: str, commit: str, base: str, wait: bool, token: str | None = None) -> dict:
    """Push exactly `commit` to main, on the owner's one-time token for
    that commit on top of `base`, to the repository the card named
    (publish_gate)."""
    site = _site(slug)
    if not (_gate.FULL_SHA_RE.match(commit or "") and _gate.FULL_SHA_RE.match(base or "")):
        raise SiteError("publish takes the full --commit and --base the owner's card named")
    with _lock(slug):
        sha, head = _publishable(slug, commit)
        if head != base:
            raise SiteError(f"GitHub's main is {head[:7]}, not the {base[:7]} the card showed: "
                            f"the site changed after the card. Run pull, commit and stage again")
        if not token_file(slug).is_file():
            raise SiteError(f"no GitHub token at {token_file(slug)}; the owner installs it. "
                            f"The commit is kept in the clone; stage it again once it is there")
        try:
            _gate.require_token(token, {"target": "site", "name": slug, "repo": site["repo"],
                                        "commit": sha, "base": base})
        except _gate.GateError as e:
            raise SiteError(str(e)) from None
        return _push_and_verify(slug, base, sha, wait)


def cmd_revert(slug: str, commit: str) -> dict:
    """Commit the undo of one published commit. Publishing it is the
    owner's click, as for any other commit."""
    if not SHA_RE.match(commit or ""):
        raise SiteError(f"--commit must be a commit id (7 to 40 hex digits), not {commit!r}")
    with _lock(slug):
        _fetch(slug)
        ahead, behind = _ahead_behind(slug)
        if behind or ahead or _changes(slug):
            raise SiteError("the clone must match GitHub before a revert: publish or discard "
                            "local changes, and pull, first")
        try:
            _git(slug, "merge-base", "--is-ancestor", commit, "HEAD")
        except SiteError:
            raise SiteError(f"{commit} is not a published commit of {slug}") from None
        _git(slug, "revert", "--no-edit", commit)
        sha = _git(slug, "rev-parse", "HEAD").strip()
        return {"status": "reverted", "site": slug, "reverted": commit[:7], "commit": sha,
                "stage": _staging_hint(slug, sha)}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Edit and publish the EA's GitHub Pages sites.", allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", allow_abbrev=False)
    for verb in ("status", "diff", "pull", "add-file", "commit", "revert", "preview", "publish"):
        s = sub.add_parser(verb, allow_abbrev=False)
        s.add_argument("--site", required=True)
        if verb in ("diff", "preview"):
            s.add_argument("--max-chars", type=int, default=20000)
        if verb == "add-file":
            s.add_argument("--from", dest="src", required=True,
                           help="a PDF or an image in a deck folder")
            s.add_argument("--to", dest="dest", required=True,
                           help="the path in the site, such as slides/L14-quantization.pdf")
        if verb == "commit":
            s.add_argument("--message", required=True)
        if verb in ("revert", "preview", "publish"):
            s.add_argument("--commit", required=True)
        if verb == "publish":
            s.add_argument("--base", required=True,
                           help="GitHub's main as the owner's card showed it")
            s.add_argument("--confirm-token", default=None,
                           help=f"the one-time token (default: ${_gate.TOKEN_ENV})")
            s.add_argument("--no-wait", action="store_true",
                           help="return after the push, without waiting for the live site")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.verb == "list":
            res = cmd_list()
        else:
            _site(args.site)
            if args.verb == "status":
                res = cmd_status(args.site)
            elif args.verb == "diff":
                res = cmd_diff(args.site, args.max_chars)
            elif args.verb == "pull":
                res = cmd_pull(args.site)
            elif args.verb == "add-file":
                res = cmd_add_file(args.site, args.src, args.dest)
            elif args.verb == "commit":
                res = cmd_commit(args.site, args.message)
            elif args.verb == "preview":
                res = cmd_preview(args.site, args.commit, args.max_chars)
            elif args.verb == "publish":
                res = cmd_publish(args.site, args.commit, args.base, not args.no_wait,
                                  token=args.confirm_token)
            else:
                res = cmd_revert(args.site, args.commit)
    except (SiteError, OSError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
