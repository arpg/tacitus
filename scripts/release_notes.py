#!/usr/bin/env python3
"""Release notes: what a Tacitus release changed since the release before it.

A release is a tag vX.Y.Z on main (docs/updates.md, "Release notes"). Its
notes list one change per first-parent commit in PREVIOUS..TAG, merge
order: a merge commit gives its pull request's title (the line after
"Merge pull request #N from ..."), a squash commit its subject without the
trailing "(#N)", and a direct commit its subject. Owner steps are the lines
that start with `Owner-steps:` in any commit message of the range (the
merge or squash commit, and the commits a merge brings in) and in the
message of TAG when it is an annotated tag: text that is part of the
signed history, never a pull request body or a Release body, which anyone
with write access to GitHub can change after the fact. The footer
convention is in CONTRIBUTING.md.

    release_notes.py v1.2.2..v1.2.3 [--json]
    release_notes.py --tag v1.2.3 [--previous v1.2.2 | --first] [--repo DIR] [--json]
    release_notes.py --tag HEAD                      # what the next release would say
    release_notes.py --tag v1.2.3 --github-repo OWNER/NAME   # CI

With no previous tag named, the previous release is the newest vX.Y.Z tag
in TAG's history below TAG; with none, TAG is the first release and every
commit in its history is a change. The Markdown is the body of the GitHub
Release the CI `release` job creates; parse_markdown reads it back for
scripts/update.py. --json prints {"tag", "previous", "changes": [{"subject",
"pr", "sha"}], "owner_steps": [{"step", "pr", "sha"}]}.

Standard library and git only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DEFAULT_GITHUB_REPO = "arpg/tacitus"
API = "https://api.github.com"
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from \S+")
SQUASH_RE = re.compile(r"^(.*\S)\s+\(#(\d+)\)$")
OWNER_STEPS_RE = re.compile(r"^owner-steps:[ \t]*(.*)$", re.I)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A step that says there is none.
NO_STEP_RE = re.compile(r"^(?:none|n/?a|-+|no)\.?$", re.I)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SUBJECT_MAX = 300
# One line of the Markdown this module writes: "- <text> ([#N](url), <sha>)",
# "- <text> ([#N](url))" or "- <text> (<sha>)".
LINE_RE = re.compile(r"^- (?P<text>.+?) \((?:\[#(?P<pr>\d+)\]\([^()\s]*\))?(?:, )?"
                     r"(?P<sha>[0-9a-f]{7,40})?\)$")
SINCE_RE = re.compile(r"^##\s+Changes(?:\s+since\s+(?P<prev>\S+))?\s*$", re.I)


class NotesError(Exception):
    pass


def version_key(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _git(repo, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C")
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=120, env=env)
    if r.returncode != 0:
        raise NotesError(f"git {args[0]}: {(r.stderr or '').strip()[:300]}")
    return r.stdout


def clean(text: str, limit: int = SUBJECT_MAX) -> str:
    """One line: control characters and runs of space collapse to one space,
    cut at `limit` with an ellipsis."""
    text = " ".join(CONTROL_RE.sub(" ", text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _commit(repo, ref: str) -> str:
    try:
        return _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip()
    except NotesError:
        raise NotesError(f"{ref} names no commit in {repo}") from None


def previous_release(repo, tag: str) -> str:
    """The newest vX.Y.Z tag in `tag`'s history, below `tag` when `tag` is a
    version and never at `tag`'s own commit; "" when there is none."""
    head = _commit(repo, tag)
    key = version_key(tag)
    best, best_key = "", None
    for t in _git(repo, "tag", "--list", "v*", "--merged", head).split():
        k = version_key(t)
        if k is None or t == tag or (key is not None and k >= key):
            continue
        if best_key is not None and k <= best_key:
            continue
        if _commit(repo, f"refs/tags/{t}") == head:
            continue
        best, best_key = t, k
    return best


def _records(text: str) -> list[list[str]]:
    return [r.lstrip("\n").split("\x00") for r in text.split("\x1e") if r.strip()]


def change_of(sha: str, message: str) -> dict:
    """{"subject", "pr", "sha"} for one first-parent commit."""
    lines = (message or "").strip().splitlines()
    subject = lines[0].strip() if lines else ""
    pr = None
    m = MERGE_PR_RE.match(subject)
    if m:
        pr = int(m.group(1))
        title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        subject = title or subject
    elif m := SQUASH_RE.match(subject):
        subject, pr = m.group(1), int(m.group(2))
    return {"subject": clean(subject) or sha[:7], "pr": pr, "sha": sha}


def owner_steps(text: str) -> list[str]:
    """The steps a message or a pull request body names: each line that
    starts with `Owner-steps:`, with the indented lines under it, or, when
    the line names nothing, the list items right under it. Fenced code and
    quoted lines are skipped, and a step of "none" is no step."""
    out: list[str] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    fenced = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = OWNER_STEPS_RE.match(line)
        if not m:
            continue
        value = m.group(1).strip()
        if value:
            while i < len(lines) and lines[i][:1] in (" ", "\t") and lines[i].strip() \
                    and not LIST_ITEM_RE.match(lines[i]):
                value += " " + lines[i].strip()
                i += 1
            found = [value]
        else:
            found = []
            while i < len(lines) and (item := LIST_ITEM_RE.match(lines[i])):
                found.append(item.group(1))
                i += 1
        out += [s for s in (clean(f) for f in found) if s and not NO_STEP_RE.match(s)]
    return out


def _norm(step: str) -> str:
    return " ".join(step.lower().split()).rstrip(".")


def tag_message(repo, tag: str) -> str:
    """The message of the annotated tag `tag` (its signature left out), or
    "" when `tag` names no annotated tag."""
    ref = f"refs/tags/{tag}"
    try:
        if _git(repo, "cat-file", "-t", ref).strip() != "tag":
            return ""
        return _git(repo, "for-each-ref", "--format=%(contents:subject)%0a%0a%(contents:body)", ref)
    except NotesError:
        return ""


def collect(repo, tag: str, previous: str | None = None, *, first: bool = False,
            warn=None) -> dict:
    """The notes for `tag`: {"tag", "previous", "changes", "owner_steps"}.
    `previous` None finds the previous release; `first` takes the whole
    history. Owner steps come from the commit messages of the range and
    the message of `tag` when it is an annotated tag."""
    _commit(repo, tag)
    if first:
        previous = ""
    elif previous is None:
        previous = previous_release(repo, tag)
    elif previous:
        _commit(repo, previous)
    span = f"{previous}..{tag}" if previous else tag
    log = _git(repo, "log", "--first-parent", "--reverse", "--format=%H%x00%P%x00%B%x1e", span)
    changes: list[dict] = []
    steps: list[dict] = []
    seen: set[str] = set()

    def add_steps(text: str, pr, sha: str) -> None:
        for s in owner_steps(text):
            if _norm(s) not in seen:
                seen.add(_norm(s))
                steps.append({"step": s, "pr": pr, "sha": sha})

    for rec in _records(log):
        if len(rec) < 3:
            continue
        sha, parents, message = rec[0].strip(), rec[1].split(), rec[2]
        change = change_of(sha, message)
        changes.append(change)
        add_steps(message, change["pr"], sha)
        if len(parents) > 1:
            # The commits the merge brings in, oldest first.
            side = _git(repo, "log", "--reverse", "--format=%H%x00%B%x1e", f"{sha}^1..{sha}^2")
            for r in _records(side):
                add_steps(r[1] if len(r) > 1 else "", change["pr"], sha)
    add_steps(tag_message(repo, tag), None, _commit(repo, tag))
    return {"tag": tag, "previous": previous or None, "changes": changes, "owner_steps": steps}


# ── Markdown ─────────────────────────────────────────────────────────

def _ref(item: dict, base: str, with_sha: bool = True) -> str:
    parts = []
    if item.get("pr") is not None:
        parts.append(f"[#{item['pr']}]({base}/pull/{item['pr']})")
    if with_sha and item.get("sha"):
        parts.append(str(item["sha"])[:7])
    return f" ({', '.join(parts)})" if parts else ""


def to_markdown(notes: dict, github_repo: str | None = None) -> str:
    """The body of the GitHub Release: the changes, the owner steps when
    there are any, and the full diff since the previous release."""
    base = f"https://github.com/{github_repo or DEFAULT_GITHUB_REPO}"
    prev, tag = notes.get("previous"), notes.get("tag")
    out = [f"## Changes since {prev}" if prev else "## Changes", ""]
    for c in notes.get("changes") or []:
        out.append(f"- {clean(c['subject'])}{_ref(c, base)}")
    if not notes.get("changes"):
        out.append(f"No change since {prev}." if prev else "No change.")
    steps = notes.get("owner_steps") or []
    if steps:
        out += ["", "## Owner steps", "",
                "Each install's owner takes these by hand after the update:", ""]
        for s in steps:
            ref = _ref(s, base, with_sha=s.get("pr") is None)
            out.append(f"- {clean(s['step'])}{ref}")
    if prev and tag:
        out += ["", f"**Full diff**: {base}/compare/{prev}...{tag}"]
    return "\n".join(out) + "\n"


def parse_markdown(text: str) -> dict:
    """{"previous", "changes", "owner_steps"} read back from a body that
    to_markdown wrote. Lines it does not recognise are skipped."""
    out = {"previous": None, "changes": [], "owner_steps": []}
    section = None
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            m = SINCE_RE.match(line)
            if m:
                section = "changes"
                out["previous"] = m.group("prev")
            elif line.lower().startswith("## owner steps"):
                section = "owner_steps"
            else:
                section = None
            continue
        if section is None or not line.startswith("- "):
            continue
        m = LINE_RE.match(line)
        if m and (m.group("pr") or m.group("sha")):
            text_, pr, sha = m.group("text"), m.group("pr"), m.group("sha") or ""
        else:
            text_, pr, sha = line[2:], None, ""
        item = {"pr": int(pr) if pr else None, "sha": sha}
        if section == "changes":
            out["changes"].append(dict(item, subject=clean(text_)))
        else:
            out["owner_steps"].append(dict(item, step=clean(text_)))
    return out


# ── GitHub ───────────────────────────────────────────────────────────

def github_slug(url: str) -> str | None:
    """OWNER/NAME of a github.com repository URL, else None."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", (url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def release_url(slug: str, tag: str) -> str:
    return f"https://github.com/{slug}/releases/tag/{tag}"


def _get_json(url: str, timeout: float = 10, auth: bool = False) -> dict:
    """A JSON object from the GitHub API; with `auth`, the token in GH_TOKEN
    or GITHUB_TOKEN goes with it (CI)."""
    import urllib.request
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tacitus-release-notes",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")) if auth else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read(4_000_000).decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("the API answered with no object")
    return doc


def fetch_pr_body(slug: str, number: int) -> str:
    return str(_get_json(f"{API}/repos/{slug}/pulls/{int(number)}", auth=True).get("body") or "")


def fetch_release(slug: str, tag: str, timeout: float = 10) -> dict:
    """{"html_url", "body"} of the GitHub Release for `tag`, asked with no
    token (an install's updater sends none). Raises what urllib raises (an
    HTTPError 404 when there is no release)."""
    from urllib.parse import quote
    doc = _get_json(f"{API}/repos/{slug}/releases/tags/{quote(tag, safe='')}", timeout=timeout)
    url = doc.get("html_url") or ""
    if not (isinstance(url, str) and url.startswith(f"https://github.com/{slug}/")):
        url = release_url(slug, tag)
    return {"html_url": url, "body": str(doc.get("body") or "")}


def _origin_slug(repo) -> str | None:
    try:
        return github_slug(_git(repo, "remote", "get-url", "origin").strip())
    except NotesError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="release notes for a Tacitus release "
                                             "(docs/updates.md, Release notes)")
    ap.add_argument("range", nargs="?", help="PREVIOUS..TAG, or TAG")
    ap.add_argument("--tag", help="the release (a tag, or any commit such as HEAD)")
    ap.add_argument("--previous", help="the release before it (default: the newest "
                                       "vX.Y.Z tag in TAG's history below TAG)")
    ap.add_argument("--first", action="store_true",
                    help="TAG is the first release: every commit in its history")
    ap.add_argument("--repo", default=".", help="the repository (default .)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--github-repo", help="OWNER/NAME for the links (default: the origin "
                                          f"remote's, else {DEFAULT_GITHUB_REPO})")
    a = ap.parse_args(argv)
    tag, previous = a.tag, a.previous
    if a.range:
        if tag:
            ap.error("give the range or --tag, not both")
        left, sep, right = a.range.partition("..")
        if sep:
            if not left or not right or right.startswith("."):
                ap.error("the range is PREVIOUS..TAG")
            previous, tag = previous or left, right
        else:
            tag = left
    if not tag:
        ap.error("name the release: PREVIOUS..TAG, TAG or --tag TAG")
    if a.first and previous:
        ap.error("--first takes no previous release")
    slug = a.github_repo or _origin_slug(a.repo) or DEFAULT_GITHUB_REPO
    try:
        notes = collect(a.repo, tag, previous, first=a.first)
    except NotesError as e:
        print(f"release_notes: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(notes, indent=1))
    else:
        sys.stdout.write(to_markdown(notes, slug))
    return 0


if __name__ == "__main__":
    sys.exit(main())
