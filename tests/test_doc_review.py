"""Working documents reviewed as GitHub pull requests (doc_review.py).

What must hold: a document goes up one sentence per line without changing
how it renders; open commits it on its own branch in ea-bot's clone, pushes
it and opens a pull request, then reads the pushed copy back; update never
forces a push; a review reads back only the owner's open threads, each with
the text it is attached to and any suggested change; the poll reports each
submitted review once and a merge once; purge deletes a local draft only
when the pull request holds the same text, and only under ~/.ea/docs.
Nothing goes through the mail mediator: the token's scope is the boundary.

git runs for real against a local bare repository standing in for GitHub;
the GitHub API is a fake.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import doc_review as dr  # noqa: E402
import mail_mediation as M  # noqa: E402

# The repository and the owner's login come from github.json on a live
# install; the tests pin their own.
dr.REPO = "o/doc-review"
dr.OWNER_LOGIN = "owner"
BOT = "review-bot"


def _run(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class FakeGitHub:
    """Pull requests and review threads in memory; file content lives in git."""

    def __init__(self) -> None:
        self.pulls: dict[int, dict] = {}
        self.graph: dict = {}
        self.replies: list[tuple[int, int, str]] = []
        self.resolved: list[str] = []

    def rest(self, method, path, body=None):
        base = f"/repos/{dr.REPO}"
        assert path.startswith(base), path
        rel = path[len(base):]
        if method == "POST" and rel == "/pulls":
            n = len(self.pulls) + 1
            self.pulls[n] = {"number": n, "html_url": f"https://github.com/{dr.REPO}/pull/{n}",
                             "head": {"ref": body["head"]}, "title": body["title"],
                             "state": "open", "merged": False}
            return self.pulls[n]
        if method == "GET" and rel.startswith("/pulls/"):
            return self.pulls[int(rel.split("/")[2])]
        if method == "POST" and rel.endswith("/replies"):
            parts = rel.split("/")
            self.replies.append((int(parts[2]), int(parts[4]), body["body"]))
            return {"id": 900, "html_url": "https://example/r"}
        raise AssertionError(f"unexpected {method} {path}")

    def graphql(self, query, variables):
        if "resolveReviewThread" in query:
            self.resolved.append(variables["id"])
            return {"resolveReviewThread": {"thread": {"id": variables["id"], "isResolved": True}}}
        return {"repository": {"pullRequest": self.graph[variables["number"]]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bare 'GitHub' repo with main, ea-bot's clone of it, and EA_DIR."""
    origin, seed, clone = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "clone"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    _run("git", "clone", "-q", str(origin), str(seed))
    for c in (["config", "user.name", "t"], ["config", "user.email", "t@t"]):
        _run("git", *c, cwd=seed)
    (seed / "README.md").write_text("# doc-review\n")
    _run("git", "add", "README.md", cwd=seed)
    _run("git", "commit", "-q", "-m", "init", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)
    _run("git", "clone", "-q", str(origin), str(clone))
    for c in (["config", "user.name", BOT], ["config", "user.email", "b@b"]):
        _run("git", *c, cwd=clone)
    ea = tmp_path / "ea"
    (ea / "docs" / "survey-paper").mkdir(parents=True)
    (ea / "state").mkdir()
    monkeypatch.setattr(dr, "clone_dir", lambda: clone)
    # GitHub is the bare repository; on a live install the URL comes from github.json.
    monkeypatch.setattr(dr, "remote_url", lambda: str(origin))
    monkeypatch.setattr(dr, "EA_DIR", ea)
    monkeypatch.setattr(dr, "registry_file", lambda: ea / "state" / "doc-reviews.json")
    return {"origin": origin, "seed": seed, "clone": clone, "ea": ea}


def _origin_file(env, branch, path):
    return subprocess.run(["git", "--git-dir", str(env["origin"]), "show", f"{branch}:{path}"],
                          capture_output=True, text=True, check=True).stdout


def _draft(env, text, name="outline.md"):
    p = env["ea"] / "docs" / "survey-paper" / name
    p.write_text(text)
    return p


def _pull(n=1, reviews=(), threads=(), comments=(), merged=False, state="OPEN"):
    return {"number": n, "title": "Year 2 outline", "url": f"https://github.com/{dr.REPO}/pull/{n}",
            "state": state, "merged": merged, "headRefName": "review/year-2-outline",
            "reviews": {"nodes": list(reviews)}, "reviewThreads": {"nodes": list(threads)},
            "comments": {"nodes": list(comments)}}


def _thread(tid, line, body, author=dr.OWNER_LOGIN, resolved=False, outdated=False,
            start=None, hunk="", orig=None):
    return {"id": tid, "isResolved": resolved, "isOutdated": outdated, "path": "docs/year-2-outline.md",
            "line": None if outdated else line, "startLine": start, "originalLine": orig or line,
            "originalStartLine": None,
            "comments": {"nodes": [{"databaseId": 70 + line, "body": body, "createdAt": "t",
                                    "diffHunk": hunk, "author": {"login": author}}]}}


# one sentence per line

def test_sembr_splits_sentences_and_leaves_blocks_alone():
    src = ("# Plan\n\nWe start in May. See e.g. Fig. 2 for J. Smith and the U.S. data! Next?\n"
           "- First item. Second part.\n  More here. And here.\n\n"
           "```\nx = 1. Not split.\n```\n| a. B | c |\n> Quote one. Quote two.\n")
    out = dr.sembr(src)
    assert out.split("\n")[2:5] == ["We start in May.",
                                    "See e.g. Fig. 2 for J. Smith and the U.S. data!", "Next?"]
    assert "- First item.\n  Second part.\n  More here.\n  And here." in out
    assert "x = 1. Not split." in out and "| a. B | c |" in out
    assert "> Quote one. Quote two." in out
    assert dr.sembr(out) == out
    # joining the new lines with spaces gives each paragraph back
    assert out.replace("\n", " ").split() == src.replace("\n", " ").split()


def test_sembr_never_starts_a_line_that_would_open_a_block():
    out = dr.sembr("It rose by 5. 2. Then it fell. # Not a heading. - not a list. Done.\n")
    for line in out.split("\n")[1:]:
        assert not dr._BLOCK_START_RE.match(line), line


def test_sembr_keeps_front_matter():
    src = "---\ntitle: A. B. Test\n---\nOne. Two.\n"
    assert dr.sembr(src) == "---\ntitle: A. B. Test\n---\nOne.\nTwo.\n"


def test_parse_suggestion():
    assert dr.parse_suggestion("tighten\n```suggestion\nShorter line.\n```") == "Shorter line."
    assert dr.parse_suggestion("no block here") is None


# open, fetch, update, purge

def test_open_pushes_a_branch_opens_a_pr_and_can_purge_the_draft(env):
    gh = FakeGitHub()
    draft = _draft(env, "# Year 2\n\nThrust A comes first. Thrust B follows.")
    res = dr.cmd_open(gh, str(draft), "Year 2 outline", thread="stream:36:topic:Year%202",
                      project="survey-paper", purge=True)
    assert res["status"] == "opened" and res["pr"] == 1 and res["verified"] is True
    assert _origin_file(env, "review/year-2-outline", "docs/year-2-outline.md") == \
        "# Year 2\n\nThrust A comes first.\nThrust B follows.\n"
    assert res["purged"] == str(draft) and not draft.exists()
    row = dr.load_registry()["prs"]["1"]
    assert row["thread"] == "stream:36:topic:Year%202" and row["state"] == "open"
    assert gh.pulls[1]["head"]["ref"] == "review/year-2-outline"
    # the clone rests clean at main, and main on GitHub is untouched
    status = subprocess.run(["git", "-C", str(env["clone"]), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status == ""
    assert _origin_file(env, "main", "README.md") == "# doc-review\n"
    with pytest.raises(dr.ReviewError, match="already under review as PR #1"):
        dr.cmd_open(gh, str(_draft(env, "x")), "Year 2 outline")


def test_purge_refuses_outside_docs_and_links(env, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")
    with pytest.raises(dr.ReviewError, match="only removes"):
        dr.cmd_open(FakeGitHub(), str(outside), "T", purge=True)
    link = env["ea"] / "docs" / "link.md"
    link.symlink_to(outside)
    with pytest.raises(dr.ReviewError, match="symlink"):
        dr.cmd_open(FakeGitHub(), str(link), "T", purge=True)
    assert outside.exists()


def test_fetch_update_and_purge_follow_the_pr(env, tmp_path):
    gh = FakeGitHub()
    draft = _draft(env, "One. Two.\n")
    dr.cmd_open(gh, str(draft), "Notes")
    out = tmp_path / "copy.md"
    assert dr.cmd_fetch(gh, 1, str(out))["status"] == "fetched"
    assert out.read_text() == "One.\nTwo.\n"
    assert dr.cmd_update(gh, 1, str(out), "same")["status"] == "unchanged"
    # the owner edits on GitHub; a later update builds on his commit
    _run("git", "fetch", "-q", "origin", cwd=env["seed"])
    _run("git", "checkout", "-q", "review/notes", cwd=env["seed"])
    (env["seed"] / "docs" / "notes.md").write_text("One.\nTwo!\n")
    _run("git", "commit", "-q", "-am", "owner edit", cwd=env["seed"])
    _run("git", "push", "-q", "origin", "review/notes", cwd=env["seed"])
    dr.cmd_fetch(gh, 1, str(out))
    assert out.read_text() == "One.\nTwo!\n"
    out.write_text("One.\nTwo!\nThree.\n")
    res = dr.cmd_update(gh, 1, str(out), "Add three")
    assert res["status"] == "updated" and res["verified"] is True
    log = subprocess.run(["git", "--git-dir", str(env["origin"]), "log", "--format=%s",
                          "review/notes"], capture_output=True, text=True).stdout.split("\n")
    assert log[:3] == ["Add three", "owner edit", "Add Notes for review"]
    # the draft no longer matches the PR, so purge keeps it
    with pytest.raises(dr.ReviewError, match="nothing deleted"):
        dr.cmd_purge(gh, 1, str(draft))
    assert draft.exists()
    draft.write_text("One. Two! Three.")
    assert dr.cmd_purge(gh, 1, str(draft))["status"] == "purged" and not draft.exists()
    gh.pulls[1]["merged"] = True
    with pytest.raises(dr.ReviewError, match="is merged"):
        dr.cmd_update(gh, 1, str(out), "late")


def test_fetch_reads_a_merged_pr_at_its_merge_commit(env, tmp_path):
    """GitHub deletes the review branch on merge; fetch and purge then read
    the file at the merge commit, which stays on main."""
    gh = FakeGitHub()
    draft = _draft(env, "One. Two!")
    dr.cmd_open(gh, str(draft), "Notes", slug="notes")
    _run("git", "fetch", "-q", "origin", cwd=env["seed"])
    _run("git", "checkout", "-q", "main", cwd=env["seed"])
    _run("git", "merge", "-q", "--no-ff", "-m", "Merge notes", "origin/review/notes",
         cwd=env["seed"])
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=env["seed"], capture_output=True,
                         text=True, check=True).stdout.strip()
    _run("git", "push", "-q", "origin", "main", cwd=env["seed"])
    _run("git", "push", "-q", "origin", "--delete", "review/notes", cwd=env["seed"])
    gh.pulls[1].update({"merged": True, "state": "closed", "merge_commit_sha": sha})
    out = tmp_path / "merged.md"
    res = dr.cmd_fetch(gh, 1, str(out))
    assert res["state"] == "merged" and out.read_text() == "One.\nTwo!\n"
    assert dr.cmd_purge(gh, 1, str(draft))["status"] == "purged" and not draft.exists()
    # an open row whose branch is gone still fails, in one line
    gh.pulls[1].update({"merged": False, "state": "open", "merge_commit_sha": None})
    with pytest.raises(dr.ReviewError, match="git fetch"):
        dr.cmd_fetch(gh, 1, str(out))


def test_update_never_forces_over_a_newer_push(env, tmp_path, monkeypatch):
    gh = FakeGitHub()
    dr.cmd_open(gh, str(_draft(env, "One.\n")), "Race")
    out = tmp_path / "copy.md"
    out.write_text("One.\nMine.\n")
    real_commit = dr._commit_and_push

    def owner_pushes_first(branch, start, path, text, message):
        _run("git", "fetch", "-q", "origin", cwd=env["seed"])
        _run("git", "checkout", "-q", branch, cwd=env["seed"])
        (env["seed"] / path).write_text("One.\nTheirs.\n")
        _run("git", "commit", "-q", "-am", "theirs", cwd=env["seed"])
        _run("git", "push", "-q", "origin", branch, cwd=env["seed"])
        return real_commit(branch, start, path, text, message)

    monkeypatch.setattr(dr, "_commit_and_push", owner_pushes_first)
    with pytest.raises(dr.ReviewError, match="changed while you edited"):
        dr.cmd_update(gh, 1, str(out), "mine")
    assert _origin_file(env, "review/race", "docs/race.md") == "One.\nTheirs.\n"


# review, reply, resolve

def test_review_returns_owner_open_threads_with_anchor_and_suggestion(env):
    gh = FakeGitHub()
    dr.cmd_open(gh, str(_draft(env, "Line one. Line two. Line three.\n")), "Year 2 outline")
    gh.graph[1] = _pull(threads=[
        _thread("T1", 2, "shorter\n```suggestion\nTwo.\n```"),
        _thread("T2", 3, "done already", resolved=True),
        _thread("T3", 1, "bot's own note", author=BOT),
        _thread("T4", 9, "was here", outdated=True, orig=2, hunk="@@ -0,0 +1,2 @@\n+Old one.\n+Old two."),
    ], reviews=[{"databaseId": 5, "state": "COMMENTED", "body": "Tighten overall.",
                 "submittedAt": "t", "author": {"login": dr.OWNER_LOGIN}}])
    res = dr.cmd_review(gh, 1)
    assert [t["thread_id"] for t in res["open_threads"]] == ["T1", "T4"]
    t1, t4 = res["open_threads"]
    assert t1["anchor"] == "Line two." and t1["suggestion"] == "Two." and t1["comment_id"] == 72
    assert t4["outdated"] is True and t4["anchor"] == "Old two."
    assert [n["body"] for n in res["review_notes"]] == ["Tighten overall."]


def test_reply_and_resolve(env):
    gh = FakeGitHub()
    assert dr.cmd_reply(gh, 1, 72, "Shortened to one clause.")["status"] == "replied"
    assert gh.replies == [(1, 72, "Shortened to one clause.")]
    assert dr.cmd_resolve(gh, 1, "PRRT_kwDO123")["status"] == "resolved"
    with pytest.raises(dr.ReviewError):
        dr.cmd_resolve(gh, 1, "bad id!")
    with pytest.raises(dr.ReviewError):
        dr.cmd_reply(gh, 1, 72, "  ")


# the poll and the registry

def test_poll_reports_each_review_once_and_the_merge(env):
    gh = FakeGitHub()
    dr.cmd_open(gh, str(_draft(env, "A. B.\n")), "Year 2 outline", thread="stream:36:topic:x")
    owner_review = {"databaseId": 11, "state": "CHANGES_REQUESTED", "body": "",
                    "submittedAt": "t", "author": {"login": dr.OWNER_LOGIN}}
    pending = {"databaseId": 12, "state": "PENDING", "body": "", "submittedAt": None,
               "author": {"login": dr.OWNER_LOGIN}}
    bot_reply = {"databaseId": 13, "state": "COMMENTED", "body": "", "submittedAt": "t",
                 "author": {"login": BOT}}
    gh.graph[1] = _pull(reviews=[owner_review, pending, bot_reply], threads=[_thread("T1", 1, "fix")])
    ev = dr.poll_reviews(gh)
    assert [(e["kind"], e["review_id"], e["open_threads"], e["thread"]) for e in ev] == \
        [("review", 11, 1, "stream:36:topic:x")]
    assert dr.poll_reviews(gh) == []
    gh.graph[1] = _pull(reviews=[owner_review], merged=True, state="MERGED")
    assert [e["kind"] for e in dr.poll_reviews(gh)] == ["merged"]
    assert dr.load_registry()["prs"]["1"]["state"] == "merged"
    assert dr.poll_reviews(gh) == []


def test_threads_follow_a_renamed_topic(env):
    dr.cmd_open(FakeGitHub(), str(_draft(env, "A.\n")), "Doc", thread="stream:36:topic:old")
    assert dr.move_thread("stream:36:topic:old", "stream:36:topic:new") == 1
    assert dr.load_registry()["prs"]["1"]["thread"] == "stream:36:topic:new"
    assert dr.set_thread(1, "stream:36:topic:later")["thread"] == "stream:36:topic:later"
    with pytest.raises(dr.ReviewError):
        dr.set_thread(1, "has space")
    assert dr.channel_of("stream:36:topic:new") == "stream:36"


# the boundary

def test_nothing_goes_through_the_mail_mediator():
    import mail_mediator as mm
    assert "doc_review" not in M.SAFE and "doc_review" not in mm.TOOLS
    assert "mail_mediation" not in (ROOT / "preprocessor" / "doc_review.py").read_text()


def test_the_repo_and_owner_login_come_from_github_json(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "github.json").write_text(
        '{"_doc": {"x": "ignored"}, "bot_login": "b", "doc_review": {"_note": "ignored", '
        '"repo": "acme/doc-review", "owner_login": "acme-owner"}}')
    monkeypatch.setattr(dr, "EA_DIR", tmp_path)
    cfg = dr._github_config()
    assert cfg["doc_review"] == {"repo": "acme/doc-review", "owner_login": "acme-owner"}
    assert cfg["bot_login"] == "b" and cfg["bot_email"] == "" and cfg["sites"] == {}
    assert "_doc" not in cfg
    monkeypatch.setattr(dr, "EA_DIR", tmp_path / "nowhere")
    assert dr._github_config() == dr.GITHUB_DEFAULTS


def test_an_unconfigured_repo_or_owner_is_refused_in_one_line(env):
    gh = FakeGitHub()
    dr.cmd_open(gh, str(_draft(env, "A. B.\n")), "Year 2 outline")
    gh.graph[1] = _pull()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dr, "OWNER_LOGIN", "")
        with pytest.raises(dr.ReviewError, match="github.json names no doc_review.owner_login; see docs/SETUP.md"):
            dr.cmd_review(gh, 1)
        with pytest.raises(dr.ReviewError, match="owner_login"):
            dr.poll_reviews(gh)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dr, "REPO", "")
        with pytest.raises(dr.ReviewError, match="github.json names no doc_review.repo; see docs/SETUP.md"):
            dr.cmd_status(gh, 1)
        with pytest.raises(dr.ReviewError, match="doc_review.repo"):
            dr.cmd_open(gh, str(_draft(env, "C.\n", name="two.md")), "Two")


def test_token_and_clone_paths_come_from_passwd_not_home(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/not-ea-bot")
    assert not str(dr.token_file()).startswith("/tmp/not-ea-bot")
    assert dr.token_file().parts[-3:] == (".config", "doc-review", "token")
    assert dr.clone_dir().name == "doc-review"
