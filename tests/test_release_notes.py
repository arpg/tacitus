"""Release notes (scripts/release_notes.py, docs/updates.md "Release notes"):
the changes between two release tags from the merge and squash commits, the
owner steps from the `Owner-steps:` lines, the Markdown of the GitHub
Release and its JSON, the first release, and the parser the updater reads a
Release body back with.

Every repository is a temp one made here; nothing reaches the network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import release_notes as rn  # noqa: E402

GIT_ID = ["-c", "user.name=Release Test", "-c", "user.email=release@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    r = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def tag(repo: Path, name: str, rev: str = "HEAD") -> None:
    git(repo, "tag", "-a", "-m", name, name, rev)


@pytest.fixture
def repo(tmp_path):
    """v0.1.0: a direct commit and a squash commit. v0.1.1: a pull request
    merged with a merge commit (an owner step in a branch commit), a squash
    commit whose body lists two owner steps, and a direct commit."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    commit(r, "a.txt", "1", "The first commit")
    commit(r, "b.txt", "1", "The squash title (#3)")
    tag(r, "v0.1.0")
    git(r, "checkout", "-q", "-b", "topic")
    commit(r, "c.txt", "1", "Work on the topic\n\nOwner-steps: run the root step once\n  with sudo")
    commit(r, "c.txt", "2", "More work on the topic")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "topic", "-m",
        "Merge pull request #12 from someone/topic\n\nThe topic's title states the mechanism")
    commit(r, "d.txt", "1", "A squashed change (#13)\n\nWhat it does.\n\nOwner-steps:\n"
                            "- add update.channel to the config\n- restart the bot\n\nClaude-Session: x")
    commit(r, "e.txt", "1", "A direct commit")
    tag(r, "v0.1.1")
    return r


def subjects(notes):
    return [(c["subject"], c["pr"]) for c in notes["changes"]]


# ── the changes ─────────────────────────────────────────────────────

def test_the_changes_since_the_previous_release_are_the_merges_in_merge_order(repo):
    notes = rn.collect(repo, "v0.1.1")
    assert notes["tag"] == "v0.1.1" and notes["previous"] == "v0.1.0"
    assert subjects(notes) == [("The topic's title states the mechanism", 12),
                               ("A squashed change", 13), ("A direct commit", None)]
    merge = git(repo, "rev-parse", "v0.1.1~2")
    assert notes["changes"][0]["sha"] == merge, "a merge's change is the merge commit"
    assert all(len(c["sha"]) == 40 for c in notes["changes"])


def test_the_first_release_takes_its_whole_history(repo):
    notes = rn.collect(repo, "v0.1.0")
    assert notes["previous"] is None
    assert subjects(notes) == [("The first commit", None), ("The squash title", 3)]
    everything = rn.collect(repo, "v0.1.1", first=True)
    assert everything["previous"] is None and len(everything["changes"]) == 5


def test_a_named_previous_release_and_head(repo):
    notes = rn.collect(repo, "v0.1.1", "v0.1.1~1")
    assert subjects(notes) == [("A direct commit", None)]
    commit(repo, "f.txt", "1", "Unreleased work (#14)")
    head = rn.collect(repo, "HEAD")
    assert head["previous"] == "v0.1.1" and subjects(head) == [("Unreleased work", 14)]
    with pytest.raises(rn.NotesError, match="names no commit"):
        rn.collect(repo, "v9.9.9")


def test_the_previous_release_skips_a_tag_at_the_same_commit_and_one_outside_history(repo):
    tag(repo, "v0.0.9", "v0.1.1")           # a hand-cut tag on the release's own commit
    git(repo, "checkout", "-q", "-b", "side", "v0.1.0")
    commit(repo, "s.txt", "1", "side work")
    tag(repo, "v0.1.5")                     # newer, but not in v0.1.1's history
    git(repo, "checkout", "-q", "main")
    assert rn.previous_release(repo, "v0.1.1") == "v0.1.0"
    assert rn.previous_release(repo, "v0.1.0") == ""


def test_a_merge_of_a_branch_with_no_pull_request_is_a_change_too(repo):
    git(repo, "checkout", "-q", "-b", "local")
    commit(repo, "g.txt", "1", "local work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "local", "-m", "Merge branch 'local'")
    notes = rn.collect(repo, "HEAD")
    assert subjects(notes) == [("Merge branch 'local'", None)]


def test_a_subject_is_one_clean_line():
    assert rn.clean("a\tb\x1b[31m  c\n d") == "a b [31m c d"
    long = rn.clean("x" * 400)
    assert len(long) == rn.SUBJECT_MAX and long.endswith("…")
    c = rn.change_of("f" * 40, "Merge pull request #7 from a/b\n\n")
    assert c == {"subject": "Merge pull request #7 from a/b", "pr": 7, "sha": "f" * 40}
    assert rn.change_of("e" * 40, "")["subject"] == "eeeeeee"
    assert rn.change_of("d" * 40, "Ledger types (#71, step a) (#90)")["pr"] == 90


# ── owner steps ─────────────────────────────────────────────────────

def test_owner_steps_come_from_merge_branch_and_squash_commits(repo):
    steps = rn.collect(repo, "v0.1.1")["owner_steps"]
    merge, squash = git(repo, "rev-parse", "v0.1.1~2"), git(repo, "rev-parse", "v0.1.1~1")
    assert steps == [
        {"step": "run the root step once with sudo", "pr": 12, "sha": merge},
        {"step": "add update.channel to the config", "pr": 13, "sha": squash},
        {"step": "restart the bot", "pr": 13, "sha": squash},
    ]
    assert rn.collect(repo, "v0.1.0")["owner_steps"] == []


def test_the_owner_steps_footer():
    text = "\n".join([
        "Subject",
        "",
        "In prose, Owner-steps: is not at the start of a line.",
        "> Owner-steps: a quoted line is not a step",
        "```",
        "Owner-steps: an example in fenced code is not a step",
        "```",
        "owner-STEPS: the key is case-insensitive",
        "Owner-steps: none",
        "Owner-steps: a step that",
        "\tgoes on below",
        "Owner-steps: the same step twice",
        "Owner-steps:",
        "1. a numbered item",
        "* a starred item",
        "",
        "- a list item after a blank line is not a step",
    ])
    assert rn.owner_steps(text) == ["the key is case-insensitive", "a step that goes on below",
                                    "the same step twice", "a numbered item", "a starred item"]
    assert rn.owner_steps("") == [] and rn.owner_steps("Owner-steps: n/a\nOwner-steps: -") == []


def test_owner_steps_come_from_the_signed_history_never_a_pull_request_body(repo):
    """A pull request body and a Release body can change after the merge;
    the commits and the tag's own message cannot. Only those name steps."""
    import inspect
    assert "pr_bodies" not in inspect.signature(rn.collect).parameters
    with pytest.raises(SystemExit):
        rn.main(["--tag", "v0.1.1", "--repo", str(repo), "--pr-bodies"])
    commit(repo, "f.txt", "1", "A change with no step")
    git(repo, "tag", "-a", "-m", "v0.1.2", "-m", "Owner-steps: rotate the relay key", "v0.1.2")
    steps = rn.collect(repo, "v0.1.2")["owner_steps"]
    assert steps == [{"step": "rotate the relay key", "pr": None,
                      "sha": git(repo, "rev-parse", "v0.1.2^{commit}")}]
    # a lightweight tag has no message
    git(repo, "tag", "v0.1.3")
    assert rn.collect(repo, "v0.1.3", previous="v0.1.2")["owner_steps"] == []
    assert rn.tag_message(repo, "HEAD") == ""


# ── Markdown and JSON ───────────────────────────────────────────────

def test_the_markdown_links_each_pull_request_and_reads_back(repo):
    notes = rn.collect(repo, "v0.1.1")
    md = rn.to_markdown(notes, "someone/tacitus")
    sha = lambda i: notes["changes"][i]["sha"][:7]
    assert md.startswith("## Changes since v0.1.0\n\n")
    assert (f"- The topic's title states the mechanism "
            f"([#12](https://github.com/someone/tacitus/pull/12), {sha(0)})") in md
    assert f"- A direct commit ({sha(2)})" in md
    assert "## Owner steps" in md
    assert "- restart the bot ([#13](https://github.com/someone/tacitus/pull/13))" in md
    assert md.rstrip().endswith("**Full diff**: https://github.com/someone/tacitus/compare/v0.1.0...v0.1.1")
    back = rn.parse_markdown(md)
    assert back["previous"] == "v0.1.0"
    assert [(c["subject"], c["pr"], c["sha"]) for c in back["changes"]] == \
        [(c["subject"], c["pr"], c["sha"][:7]) for c in notes["changes"]]
    assert [s["step"] for s in back["owner_steps"]] == [s["step"] for s in notes["owner_steps"]]


def test_the_first_release_markdown_has_no_previous_and_no_diff(repo):
    md = rn.to_markdown(rn.collect(repo, "v0.1.0"))
    assert md.startswith("## Changes\n\n") and "Full diff" not in md and "Owner steps" not in md
    assert "[#3](https://github.com/arpg/tacitus/pull/3)" in md, "the default repository"
    assert rn.parse_markdown(md)["previous"] is None
    empty = rn.to_markdown({"tag": "v0.1.1", "previous": "v0.1.0", "changes": [], "owner_steps": []})
    assert "No change since v0.1.0." in empty and rn.parse_markdown(empty)["changes"] == []


def test_parse_reads_a_subject_that_ends_in_parentheses_and_skips_other_lines():
    md = ("Intro.\n\n## Changes since v1.0.0\n\n"
          "- Types declare traits (#71, step a) ([#90](https://github.com/o/n/pull/90), a0be445)\n"
          "- Bump to (deadbee) (1234567)\n"
          "- A line someone edited by hand\n\n"
          "## Something else\n\n- not a change\n")
    back = rn.parse_markdown(md)
    assert [(c["subject"], c["pr"], c["sha"]) for c in back["changes"]] == [
        ("Types declare traits (#71, step a)", 90, "a0be445"),
        ("Bump to (deadbee)", None, "1234567"),
        ("A line someone edited by hand", None, ""),
    ]


def test_the_cli_prints_markdown_and_json(repo, capsys):
    assert rn.main(["v0.1.0..v0.1.1", "--repo", str(repo), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"tag", "previous", "changes", "owner_steps"}
    assert doc["tag"] == "v0.1.1" and doc["previous"] == "v0.1.0"
    assert all(set(c) == {"subject", "pr", "sha"} for c in doc["changes"])
    assert doc["changes"][0]["pr"] == 12 and len(doc["owner_steps"]) == 3
    assert rn.main(["--tag", "v0.1.0", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["previous"] is None
    assert rn.main(["v0.1.1", "--repo", str(repo), "--github-repo", "o/n"]) == 0
    assert "## Changes since v0.1.0" in capsys.readouterr().out
    assert rn.main(["--tag", "v7.7.7", "--repo", str(repo)]) == 1
    assert "names no commit" in capsys.readouterr().err
    for bad in ([], ["v0.1.0..", "--repo", str(repo)], ["v0.1.0...v0.1.1"],
                ["v0.1.1", "--tag", "v0.1.1"], ["v0.1.1", "--first", "--previous", "v0.1.0"]):
        with pytest.raises(SystemExit) as e:
            rn.main(bad)
        assert e.value.code == 2, bad


def test_the_script_runs_isolated_on_the_standard_library(repo):
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "release_notes.py"),
                        "--repo", str(repo), "v0.1.1"], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    assert r.returncode == 0, r.stderr
    assert "## Changes since v0.1.0" in r.stdout and "## Owner steps" in r.stdout


# ── GitHub ──────────────────────────────────────────────────────────

def test_the_github_slug_and_urls():
    for url in ("https://github.com/arpg/tacitus.git", "https://github.com/arpg/tacitus",
                "git@github.com:arpg/tacitus.git", "ssh://git@github.com/arpg/tacitus.git"):
        assert rn.github_slug(url) == "arpg/tacitus", url
    for url in ("/srv/git/tacitus.git", "https://gitlab.com/arpg/tacitus.git",
                "https://github.com/arpg", "https://github.com.evil.example/arpg/tacitus", ""):
        assert rn.github_slug(url) is None, url
    assert rn.release_url("arpg/tacitus", "v1.2.3") == "https://github.com/arpg/tacitus/releases/tag/v1.2.3"


def test_fetch_release_keeps_only_a_link_into_the_repository(monkeypatch):
    seen = []
    monkeypatch.setattr(rn, "_get_json", lambda url, timeout=10, auth=False: seen.append((url, auth)) or
                        {"html_url": "https://elsewhere.example/x", "body": "## Changes\n"})
    got = rn.fetch_release("arpg/tacitus", "v1.2.3")
    assert seen == [("https://api.github.com/repos/arpg/tacitus/releases/tags/v1.2.3", False)]
    assert got == {"html_url": "https://github.com/arpg/tacitus/releases/tag/v1.2.3", "body": "## Changes\n"}


def test_only_the_ci_path_sends_a_token(monkeypatch):
    import io
    import urllib.request
    sent = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        sent.append((req.full_url, req.get_header("Authorization")))
        return Answer(b'{"body": "Owner-steps: x", "html_url": ""}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GH_TOKEN", "test-token-value")
    assert rn.fetch_pr_body("o/n", 5) == "Owner-steps: x"
    rn.fetch_release("o/n", "v1.0.0")
    assert sent == [("https://api.github.com/repos/o/n/pulls/5", "Bearer test-token-value"),
                    ("https://api.github.com/repos/o/n/releases/tags/v1.0.0", None)]
