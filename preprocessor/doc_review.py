#!/usr/bin/env python3
"""doc_review: working documents reviewed as GitHub pull requests.

The EA writes a working document; this tool puts it in the owner's private
review repo as a pull request that adds the whole file, so every line can
take a comment. The owner comments (or suggests exact wording) and submits a
review; the EA fetches the review's open threads, makes the changes in one
commit, replies on each thread and resolves it. Merging is the owner's step,
and the merged file is the finished Markdown.

It runs as ea-bot, in ea-bot's own clone of the repo (~/doc-review), with
the bot account's fine-grained token (~/.config/doc-review/token): one
repository, contents and pull requests only. The repository and the owner's
login come from ~/.ea/config/github.json (`doc_review.repo`,
`doc_review.owner_login`; see docs/SETUP.md). That token cannot create or delete a
repository, change a setting, add a collaborator or reach any other
repository, and that is the security boundary. This tool is a convenience
over git and the GitHub API, not a gate. git runs through safe_git.py, so
nothing the clone's .git or its .gitattributes says runs or changes what
git prints, and fetch and push go to the repository github.json names,
never to the clone's remote.

    doc_review.py open    --file F --title T [--slug S] [--thread KEY] [--project ID] [--purge]
    doc_review.py fetch   --pr N --out F
    doc_review.py update  --pr N --file F --message M
    doc_review.py review  --pr N
    doc_review.py reply   --pr N --comment-id C --body B
    doc_review.py resolve --pr N --thread-id T
    doc_review.py status  [--pr N]
    doc_review.py purge   --pr N --file F
    doc_review.py poll                          the bot's watcher: new reviews, merges
    doc_review.py set-thread  --pr N --thread KEY
    doc_review.py move-thread --old KEY --new KEY

A document is kept one sentence per line (`sembr`): a comment then anchors to
one sentence, and Markdown renders the file as before, since a single line
break inside a paragraph is a space. `purge` (or `open --purge`) deletes the
EA's local draft under ~/.ea/docs once the pull request holds the same text.

Output is one JSON object on stdout. A refusal is {"status": "error", ...} on
stderr with exit 2.
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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

try:
    import safe_git as _sg
except ModuleNotFoundError:  # imported as preprocessor.doc_review
    from preprocessor import safe_git as _sg

BASE_BRANCH = "main"
DOC_DIR = "docs"
API = "https://api.github.com"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
THREAD_KEY_RE = re.compile(r"^[^\s\]]{1,400}$")
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
REPO: str = str(_GH["doc_review"]["repo"] or "")
OWNER_LOGIN: str = str(_GH["doc_review"]["owner_login"] or "")
# The commit identity: github.json bot_login and bot_email, or the clone's
# own user.name and user.email when github.json names none.
BOT_NAME: str = str(_GH["bot_login"] or "")
BOT_EMAIL: str = str(_GH["bot_email"] or "")
GITHUB_GIT = "https://github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


def _repo() -> str:
    if not REPO or "/" not in REPO:
        raise ReviewError("github.json names no doc_review.repo; see docs/SETUP.md")
    if not REPO_RE.match(REPO) or ".." in REPO:
        raise ReviewError(f"github.json doc_review.repo must be owner/name, not {REPO!r}")
    return REPO


def remote_url() -> str:
    """Where fetch and push go: the repository github.json names."""
    return f"{GITHUB_GIT}/{_repo()}.git"


def _owner_login() -> str:
    if not OWNER_LOGIN:
        raise ReviewError("github.json names no doc_review.owner_login; see docs/SETUP.md")
    return OWNER_LOGIN


class ReviewError(Exception):
    """A refusal or a git or GitHub failure, reported as the tool's error shape."""


def _home() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def token_file() -> Path:
    return _home() / ".config" / "doc-review" / "token"


def clone_dir() -> Path:
    return _home() / "doc-review"


def registry_file() -> Path:
    return EA_DIR / "state" / "doc-reviews.json"


# --- one sentence per line --------------------------------------------------

_ABBREV = {
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "al.", "approx.", "resp.", "viz.",
    "dr.", "mr.", "mrs.", "ms.", "prof.", "st.", "jr.", "sr.", "no.", "nos.",
    "fig.", "figs.", "eq.", "eqs.", "sec.", "secs.", "ch.", "ref.", "refs.",
    "vol.", "pp.", "p.", "ed.", "eds.", "inc.", "ltd.", "co.", "corp.",
    "u.s.", "u.k.", "a.m.", "p.m.", "jan.", "feb.", "mar.", "apr.", "jun.",
    "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
}
_CLOSERS = "\"'”’)]*_"
_OPENERS = "\"'“‘([*_"
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)(\S.*)$")
# A line starting with one of these is not paragraph text: leave it as is.
_SKIP_RE = re.compile(r"^\s{0,3}(#|>|\||<|\$\$|\[[^\]]+\]:|[-*_=]{3,}\s*$)")
# A new line must never start with one of these, or it would open a block.
_BLOCK_START_RE = re.compile(r"^(#{1,6}(\s|$)|>|[-*+](\s|$)|\d{1,9}[.)](\s|$)|\||<|```|~~~|"
                             r"[-=_*]{3,}\s*$|\[[^\]]+\]:|\$\$)")


def _sentence_end(word: str, nxt: str) -> bool:
    core = word.rstrip(_CLOSERS)
    if not core or core[-1] not in ".!?":
        return False
    bare = core.lstrip(_OPENERS)
    if bare.lower() in _ABBREV:
        return False
    if re.fullmatch(r"[A-Z]\.", bare):
        return False  # an initial: "J. Smith"
    if re.fullmatch(r"(?:[A-Za-z]\.){2,}", bare):
        return False  # "U.S.", "e.g." and the like
    first = nxt.lstrip(_OPENERS)[:1]
    if not first or not (first.isupper() or first.isdigit()):
        return False
    return not _BLOCK_START_RE.match(nxt)


def _split_text(text: str, indent: str) -> str:
    """One sentence per line for one line of paragraph text. Breaks only at
    a single space, so joining the lines with spaces gives the text back."""
    words = text.split(" ")
    out = [words[0]]
    for i in range(1, len(words)):
        prev, word = words[i - 1], words[i]
        if prev and word and _sentence_end(prev, word):
            out.append("\n" + indent + word)
        else:
            out.append(" " + word)
    return "".join(out)


def sembr(markdown: str) -> str:
    """The document with each sentence of its paragraphs on its own line.
    Code, headings, tables, quotes, HTML and front matter are left as they
    are; running it twice changes nothing."""
    lines = markdown.split("\n")
    out: list[str] = []
    fence: str | None = None
    list_indent: int | None = None
    front = bool(lines) and lines[0].strip() == "---"
    for n, line in enumerate(lines):
        if front:
            out.append(line)
            if n > 0 and line.strip() in ("---", "..."):
                front = False
            continue
        m = _FENCE_RE.match(line)
        if fence is not None:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        if not line.strip():
            out.append(line)
            continue
        lm = _LIST_RE.match(line)
        if lm:
            lead, marker, gap, body = lm.groups()
            list_indent = len(lead) + len(marker) + len(gap)
            out.append(lead + marker + gap + _split_text(body, " " * list_indent))
            continue
        indent = len(line) - len(line.lstrip(" "))
        if list_indent is not None and indent >= list_indent:
            out.append(" " * indent + _split_text(line[indent:], " " * indent))
            continue
        list_indent = None
        if indent >= 4 or "\t" in line[:indent + 1] or _SKIP_RE.match(line) or " | " in line:
            out.append(line)
            continue
        out.append(" " * indent + _split_text(line[indent:], " " * indent))
    return "\n".join(out)


def _prepared(file: str) -> str:
    text = sembr(Path(file).read_text())
    return text if text.endswith("\n") else text + "\n"


def parse_suggestion(body: str) -> str | None:
    """The replacement text of a GitHub suggested change, or None."""
    m = re.search(r"```suggestion[^\n]*\n(.*?)```", body or "", re.S)
    if not m:
        return None
    text = m.group(1)
    return text[:-1] if text.endswith("\n") else text


# --- git (ea-bot's clone) ---------------------------------------------------

def _git(*args: str) -> str:
    """git in ea-bot's clone, through safe_git.run."""
    clone = clone_dir()
    if not (clone / ".git").is_dir():
        raise ReviewError(f"no clone of {_repo()} at {clone}")
    identity = (BOT_NAME, BOT_EMAIL) if BOT_NAME and BOT_EMAIL else None
    helper = _sg.credential_helper(token_file(), "x-access-token")
    try:
        r = _sg.run(clone, *args, identity=identity, credentials={GITHUB_GIT: helper},
                    timeout=120)
    except _sg.GitError as e:
        raise ReviewError(str(e)) from None
    if r.returncode != 0:
        raise ReviewError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _remote_text(branch: str, path: str) -> str:
    """The file as the pull request's branch holds it on GitHub now."""
    _git("fetch", "-q", remote_url(), f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return _git("cat-file", "blob", f"origin/{branch}:{path}")


def _commit_and_push(branch: str, start: str, path: str, text: str, message: str) -> str:
    """Write the file on `branch` (created from `start`, the branch as GitHub
    has it), commit and push. A push that GitHub refuses, because the branch
    moved meanwhile, is an error, never a force. The clone is left detached
    at main."""
    _git("checkout", "-q", "-B", branch, start)
    try:
        target = clone_dir() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git("add", path)
        _git("commit", "-q", "-m", message)
        try:
            _git("push", "-q", remote_url(), f"HEAD:refs/heads/{branch}")
        except ReviewError as e:
            raise ReviewError(f"the pull request changed while you edited it; fetch it "
                              f"again and redo the change ({e})") from e
        return _git("rev-parse", "HEAD").strip()
    finally:
        _git("checkout", "-q", "--detach", f"origin/{BASE_BRANCH}")


# --- GitHub -----------------------------------------------------------------

class GitHub:
    """REST and GraphQL calls with the bot account's token."""

    def __init__(self, token: str | None = None) -> None:
        if token is None:
            try:
                token = token_file().read_text().strip()
            except OSError as e:
                raise ReviewError(f"no GitHub token at {token_file()}: {e.strerror}") from e
        if not token:
            raise ReviewError(f"the GitHub token file {token_file()} is empty")
        self._token = token

    def _call(self, method: str, url: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ea-doc-review",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("message", "")
            except ValueError:
                msg = ""
            raise ReviewError(f"GitHub {method} {url.removeprefix(API)}: {e.code} {msg}".strip()) from e
        except urllib.error.URLError as e:
            raise ReviewError(f"GitHub unreachable: {e.reason}") from e
        return json.loads(raw) if raw else {}

    def rest(self, method: str, path: str, body=None):
        return self._call(method, API + path, body)

    def graphql(self, query: str, variables: dict) -> dict:
        res = self._call("POST", API + "/graphql", {"query": query, "variables": variables})
        if res.get("errors"):
            raise ReviewError("GitHub GraphQL: " + "; ".join(
                str(e.get("message")) for e in res["errors"]))
        return res.get("data") or {}


def _repo_path(suffix: str) -> str:
    return f"/repos/{_repo()}{suffix}"


# --- registry ---------------------------------------------------------------

def load_registry() -> dict:
    try:
        data = json.loads(registry_file().read_text())
    except (OSError, ValueError):
        return {"prs": {}}
    if not isinstance(data, dict):
        return {"prs": {}}
    data.setdefault("prs", {})
    return data


def save_registry(data: dict) -> None:
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _registry_lock():
    """Serialize read-modify-write of the registry: the watcher's poll and
    an EA turn can run at the same time."""
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def _update_rows(changes: dict[str, dict]) -> None:
    """Merge field changes into existing rows, under the lock."""
    with _registry_lock():
        data = load_registry()
        for num, fields in changes.items():
            if num in data["prs"]:
                data["prs"][num].update(fields)
        save_registry(data)


def channel_of(thread: str) -> str:
    """The channel a thread key names (a Zulip topic key), or ''."""
    return thread.split(":topic:", 1)[0] if ":topic:" in (thread or "") else ""


def set_thread(pr: int, thread: str) -> dict:
    if not THREAD_KEY_RE.match(thread or ""):
        raise ReviewError("--thread must be a thread key with no spaces")
    _update_rows({str(int(pr)): {"thread": thread}})
    return {"status": "ok", "pr": int(pr), "thread": thread}


def move_thread(old: str, new: str) -> int:
    """Point reviews whose conversation moved (a renamed topic) at the new
    key; the number of reviews changed."""
    if not registry_file().exists():
        return 0
    with _registry_lock():
        data = load_registry()
        n = 0
        for row in data["prs"].values():
            if row.get("thread") == old:
                row["thread"] = new
                n += 1
        if n:
            save_registry(data)
    return n


def _entry(gh: GitHub, pr: int) -> dict:
    """The registry row for a PR, completed from GitHub for one opened
    elsewhere."""
    row = dict(load_registry()["prs"].get(str(pr)) or {})
    info = gh.rest("GET", _repo_path(f"/pulls/{int(pr)}"))
    row["branch"] = (info.get("head") or {}).get("ref") or row.get("branch", "")
    row["url"] = info.get("html_url") or row.get("url", "")
    row["title"] = info.get("title") or row.get("title", "")
    row["state"] = "merged" if info.get("merged") else str(info.get("state") or "")
    if not row.get("path"):
        files = gh.rest("GET", _repo_path(f"/pulls/{int(pr)}/files"))
        mds = [f.get("filename") for f in files if str(f.get("filename", "")).endswith(".md")]
        if not mds:
            raise ReviewError(f"PR #{pr} changes no Markdown file")
        row["path"] = mds[0]
    return row


# --- verbs ------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return s or "document"


def _draft_path(file: str) -> Path:
    """A local draft that purge may delete: a .md file under ~/.ea/docs,
    never a link."""
    docs = (EA_DIR / "docs").resolve()
    p = Path(file)
    if p.is_symlink():
        raise ReviewError("refusing a symlink")
    real = p.resolve()
    if docs not in real.parents or real.suffix != ".md" or not real.is_file():
        raise ReviewError(f"purge only removes a .md file under {docs}")
    return real


def cmd_open(gh: GitHub, file: str, title: str, slug: str = "", thread: str = "",
             project: str = "", purge: bool = False) -> dict:
    title = " ".join((title or "").split())
    if not title:
        raise ReviewError("--title is required")
    slug = _slug(slug or title)
    if not SLUG_RE.match(slug):
        raise ReviewError(f"bad slug {slug!r}")
    if thread and not THREAD_KEY_RE.match(thread):
        raise ReviewError("--thread must be a thread key with no spaces")
    draft = _draft_path(file) if purge else None
    for num, row in load_registry()["prs"].items():
        if row.get("slug") == slug and row.get("state") == "open":
            raise ReviewError(f"{slug} is already under review as PR #{num} ({row.get('url')}); "
                              f"use update --pr {num}")
    text = _prepared(file)
    branch, path = f"review/{slug}", f"{DOC_DIR}/{slug}.md"
    _git("fetch", "-q", "--prune", remote_url(), "+refs/heads/*:refs/remotes/origin/*")
    if _git("ls-remote", "--heads", remote_url(), branch).strip():
        raise ReviewError(f"branch {branch} already exists; pass another --slug")
    _commit_and_push(branch, f"origin/{BASE_BRANCH}", path, text, f"Add {title} for review")
    body = ("Working document from the EA, one sentence per line.\n\n"
            "- Comment on any line, or use a suggested change for exact wording.\n"
            "- Use **Start a review**, then **Submit review** when a batch is ready. The EA "
            "makes the changes in one commit, replies on each thread and resolves it.\n"
            "- Merge when the document is final. The merged file is the finished Markdown.")
    if project:
        body += f"\n\nProject: `{project}`"
    pull = gh.rest("POST", _repo_path("/pulls"),
                   {"title": title, "head": branch, "base": BASE_BRANCH, "body": body})
    number = int(pull["number"])
    with _registry_lock():
        reg = load_registry()
        reg["prs"][str(number)] = {
            "title": title, "slug": slug, "path": path, "branch": branch,
            "url": pull.get("html_url", ""), "thread": thread, "project": project,
            "opened_at": time.time(), "last_review_id": 0, "state": "open"}
        save_registry(reg)
    verified = _remote_text(branch, path) == text
    res = {"status": "opened", "pr": number, "url": pull.get("html_url", ""), "path": path,
           "branch": branch, "lines": text.count("\n"), "verified": verified}
    if draft is not None and verified:
        draft.unlink()
        res["purged"] = str(draft)
    return res


def cmd_fetch(gh: GitHub, pr: int, out: str) -> dict:
    row = _entry(gh, pr)
    text = _remote_text(row["branch"], row["path"])
    Path(out).write_text(text)
    return {"status": "fetched", "pr": pr, "path": row["path"],
            "bytes": len(text.encode("utf-8")), "out": out, "state": row["state"]}


def cmd_update(gh: GitHub, pr: int, file: str, message: str) -> dict:
    row = _entry(gh, pr)
    if row["state"] != "open":
        raise ReviewError(f"PR #{pr} is {row['state']}; open a new review instead")
    text = _prepared(file)
    if _remote_text(row["branch"], row["path"]) == text:
        return {"status": "unchanged", "pr": pr, "url": row["url"]}
    commit = _commit_and_push(row["branch"], f"origin/{row['branch']}", row["path"], text,
                              (message or "Revise for review").strip()[:200])
    return {"status": "updated", "pr": pr, "commit": commit, "url": row["url"],
            "verified": _remote_text(row["branch"], row["path"]) == text}


def cmd_purge(gh: GitHub, pr: int, file: str) -> dict:
    """Delete the EA's local draft once the PR holds the same text."""
    draft = _draft_path(file)
    row = _entry(gh, pr)
    if _remote_text(row["branch"], row["path"]) != _prepared(str(draft)):
        raise ReviewError(f"PR #{pr} does not hold this file's text; nothing deleted")
    draft.unlink()
    return {"status": "purged", "pr": pr, "file": str(draft)}


_REVIEW_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title url state merged headRefName
      reviews(last: 50) { nodes { databaseId state body submittedAt author { login } } }
      reviewThreads(first: 100) { nodes {
        id isResolved isOutdated path line startLine originalLine originalStartLine
        comments(first: 50) { nodes { databaseId body createdAt diffHunk author { login } } }
      } }
      comments(last: 50) { nodes { databaseId body createdAt author { login } } }
    }
  }
}"""


def _pull(gh: GitHub, pr: int) -> dict:
    owner, name = _repo().split("/", 1)
    data = gh.graphql(_REVIEW_QUERY, {"owner": owner, "name": name, "number": int(pr)})
    pull = ((data.get("repository") or {}).get("pullRequest")) or None
    if pull is None:
        raise ReviewError(f"no PR #{pr} in {_repo()}")
    return pull


def _login(node: dict) -> str:
    return str(((node or {}).get("author") or {}).get("login") or "")


def _anchor(lines: list[str], thread: dict) -> str:
    """The text a thread is attached to: its lines in the current file, or
    for an outdated thread the lines it was left on."""
    end = thread.get("line")
    if end and not thread.get("isOutdated"):
        start = thread.get("startLine") or end
        return "\n".join(lines[int(start) - 1:int(end)])
    comments = (thread.get("comments") or {}).get("nodes") or []
    hunk = (comments[0].get("diffHunk") if comments else "") or ""
    tail = [ln[1:] for ln in hunk.splitlines() if ln[:1] in "+ "]
    span = (thread.get("originalLine") or 0) - (thread.get("originalStartLine")
                                               or thread.get("originalLine") or 0) + 1
    return "\n".join(tail[-max(1, span):])


def cmd_review(gh: GitHub, pr: int) -> dict:
    owner = _owner_login()
    row = _entry(gh, pr)
    pull = _pull(gh, pr)
    lines = _remote_text(row["branch"], row["path"]).split("\n")
    threads = []
    for t in (pull.get("reviewThreads") or {}).get("nodes") or []:
        comments = (t.get("comments") or {}).get("nodes") or []
        if t.get("isResolved") or not comments or _login(comments[0]) != owner:
            continue
        first = comments[0]
        threads.append({
            "thread_id": t.get("id"), "comment_id": first.get("databaseId"),
            "path": t.get("path"), "line": t.get("line"), "start_line": t.get("startLine"),
            "outdated": bool(t.get("isOutdated")), "anchor": _anchor(lines, t),
            "comment": first.get("body") or "",
            "suggestion": parse_suggestion(first.get("body") or ""),
            "replies": [{"author": _login(c), "body": c.get("body") or ""}
                        for c in comments[1:]]})
    notes = [{"review_id": r.get("databaseId"), "state": r.get("state"),
              "submitted_at": r.get("submittedAt"), "body": r.get("body")}
             for r in (pull.get("reviews") or {}).get("nodes") or []
             if _login(r) == owner and (r.get("body") or "").strip()]
    general = [{"comment_id": c.get("databaseId"), "created_at": c.get("createdAt"),
                "body": c.get("body")}
               for c in (pull.get("comments") or {}).get("nodes") or []
               if _login(c) == owner]
    return {"status": "ok", "pr": pr, "title": pull.get("title"), "url": pull.get("url"),
            "state": row["state"], "path": row["path"],
            "open_threads": threads, "review_notes": notes, "pr_comments": general}


def cmd_reply(gh: GitHub, pr: int, comment_id: int, body: str) -> dict:
    body = (body or "").strip()
    if not body:
        raise ReviewError("--body is empty")
    res = gh.rest("POST", _repo_path(f"/pulls/{int(pr)}/comments/{int(comment_id)}/replies"),
                  {"body": body})
    return {"status": "replied", "pr": pr, "comment_id": res.get("id"), "url": res.get("html_url")}


def cmd_resolve(gh: GitHub, pr: int, thread_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_=-]{4,120}", thread_id or ""):
        raise ReviewError("bad --thread-id")
    data = gh.graphql("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
                      "{ thread { id isResolved } } }", {"id": thread_id})
    th = ((data.get("resolveReviewThread") or {}).get("thread")) or {}
    return {"status": "resolved" if th.get("isResolved") else "unresolved", "pr": pr,
            "thread_id": thread_id}


def cmd_status(gh: GitHub, pr: int | None = None) -> dict:
    owner = _owner_login()
    reg = load_registry()["prs"]
    nums = [int(pr)] if pr else sorted(int(n) for n, r in reg.items() if r.get("state") == "open")
    out = []
    for n in nums:
        pull = _pull(gh, n)
        threads = (pull.get("reviewThreads") or {}).get("nodes") or []
        owner_reviews = [r for r in (pull.get("reviews") or {}).get("nodes") or []
                         if _login(r) == owner and r.get("state") != "PENDING"]
        last = owner_reviews[-1] if owner_reviews else {}
        out.append({"pr": n, "title": pull.get("title"), "url": pull.get("url"),
                    "state": "merged" if pull.get("merged") else str(pull.get("state") or "").lower(),
                    "open_threads": sum(1 for t in threads if not t.get("isResolved")),
                    "last_review": last.get("submittedAt"),
                    "last_review_state": last.get("state"),
                    "thread": (reg.get(str(n)) or {}).get("thread", "")})
    return {"status": "ok", "reviews": out}


def poll_reviews(gh: GitHub | None = None) -> list[dict]:
    """Owner reviews submitted since the last poll, and merges or closes,
    for every open review in the registry. Records what it saw."""
    open_rows = {n: r for n, r in load_registry()["prs"].items() if r.get("state") == "open"}
    if not open_rows:
        return []
    owner = _owner_login()
    gh = gh or GitHub()
    events: list[dict] = []
    changes: dict[str, dict] = {}
    for num, row in open_rows.items():
        pull = _pull(gh, int(num))
        base = {"pr": int(num), "title": pull.get("title") or row.get("title", ""),
                "url": pull.get("url") or row.get("url", ""), "thread": row.get("thread", ""),
                "project": row.get("project", "")}
        reviews = [r for r in (pull.get("reviews") or {}).get("nodes") or []
                   if _login(r) == owner and r.get("state") != "PENDING"
                   and int(r.get("databaseId") or 0) > int(row.get("last_review_id") or 0)]
        threads = (pull.get("reviewThreads") or {}).get("nodes") or []
        open_n = sum(1 for t in threads if not t.get("isResolved")
                     and _login(((t.get("comments") or {}).get("nodes") or [{}])[0]) == owner)
        if reviews:
            last_id = int(reviews[-1].get("databaseId") or 0)
            changes.setdefault(num, {})["last_review_id"] = last_id
            events.append({**base, "kind": "review", "review_id": last_id,
                           "state": reviews[-1].get("state"), "open_threads": open_n,
                           "has_note": any((r.get("body") or "").strip() for r in reviews)})
        if pull.get("merged") or str(pull.get("state") or "").upper() == "CLOSED":
            state = "merged" if pull.get("merged") else "closed"
            changes.setdefault(num, {})["state"] = state
            events.append({**base, "kind": state})
    if changes:
        _update_rows(changes)
    return events


# --- CLI --------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="doc_review.py", description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("open", allow_abbrev=False)
    s.add_argument("--file", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--slug", default="")
    s.add_argument("--thread", default="")
    s.add_argument("--project", default="")
    s.add_argument("--purge", action="store_true")
    s = sub.add_parser("fetch", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("update", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--message", default="")
    s = sub.add_parser("review", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s = sub.add_parser("reply", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--comment-id", type=int, required=True)
    s.add_argument("--body", required=True)
    s = sub.add_parser("resolve", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--thread-id", required=True)
    s = sub.add_parser("status", allow_abbrev=False)
    s.add_argument("--pr", type=int, default=None)
    s = sub.add_parser("purge", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--file", required=True)
    sub.add_parser("poll", allow_abbrev=False)
    s = sub.add_parser("set-thread", allow_abbrev=False)
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--thread", required=True)
    s = sub.add_parser("move-thread", allow_abbrev=False)
    s.add_argument("--old", required=True)
    s.add_argument("--new", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.verb == "set-thread":
            res = set_thread(args.pr, args.thread)
        elif args.verb == "move-thread":
            res = {"status": "ok", "moved": move_thread(args.old, args.new)}
        elif args.verb == "poll":
            res = {"status": "ok", "events": poll_reviews()}
        else:
            _repo()
            _owner_login()
            gh = GitHub()
            if args.verb == "open":
                res = cmd_open(gh, args.file, args.title, args.slug, args.thread,
                               args.project, args.purge)
            elif args.verb == "fetch":
                res = cmd_fetch(gh, args.pr, args.out)
            elif args.verb == "update":
                res = cmd_update(gh, args.pr, args.file, args.message)
            elif args.verb == "purge":
                res = cmd_purge(gh, args.pr, args.file)
            elif args.verb == "review":
                res = cmd_review(gh, args.pr)
            elif args.verb == "reply":
                res = cmd_reply(gh, args.pr, args.comment_id, args.body)
            elif args.verb == "resolve":
                res = cmd_resolve(gh, args.pr, args.thread_id)
            else:
                res = cmd_status(gh, args.pr)
    except (ReviewError, OSError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
