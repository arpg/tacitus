"""The EA's GitHub Pages sites (site_tools.py).

What must hold: commit commits every change in ea-bot's clone as the bot
account and refuses when GitHub has commits the clone lacks and when a
changed page has an em-dash; publish runs only with the owner's one-time
token for exactly that commit on top of the GitHub head the card showed,
pushes that commit to main without a force, and reports live only once the
site serves each pushed file as committed; pull only fast-forwards; revert
commits the undo of one published commit, which publishes the same way; a hook
written into the clone never runs; the clone and token
paths come from the passwd entry, not $HOME; every site uses the bot
account's one token; the sites and the account come from github.json. Only lines a change adds are
checked for an em-dash. A Jekyll site counts as live once its build run
on the pushed commit succeeds and the files Jekyll copies unchanged are
served. add-file copies a PDF or an image from a deck folder, and nothing
from anywhere else, into the clone.

git runs for real against a local bare repository standing in for GitHub;
the live site is a fake.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import publish_gate as gate  # noqa: E402
import resident_io  # noqa: E402
import site_tools as st  # noqa: E402

SLUG = "t-site"
BOT = "t-bot"
# The bot account comes from github.json on a live install; the tests pin their own.
st.BOT_NAME = BOT
st.BOT_EMAIL = "1+t-bot@users.noreply.github.com"
st.BOT_TOKEN_DIR = BOT


def _run(*cmd, cwd=None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def _owner_edit(seed: Path, name: str, text: str) -> None:
    """Someone other than the EA pushes an edit to GitHub."""
    _run("git", "pull", "-q", "--ff-only", cwd=seed)
    (seed / name).write_text(text)
    _run("git", "add", name, cwd=seed)
    _run("git", "commit", "-q", "-m", f"owner edits {name}", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bare 'GitHub' repo with the page, ea-bot's clone, and a live site
    that serves whatever the bare repo's main holds."""
    origin, seed, clone = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "clone"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    _run("git", "clone", "-q", str(origin), str(seed))
    for c in (["config", "user.name", "owner"], ["config", "user.email", "o@o"]):
        _run("git", *c, cwd=seed)
    (seed / "index.html").write_text("<p>v1</p>\n")
    (seed / ".nojekyll").write_text("")
    _run("git", "add", "-A", cwd=seed)
    _run("git", "commit", "-q", "-m", "init", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)
    _run("git", "clone", "-q", str(origin), str(clone))
    token = tmp_path / "token"
    token.write_text("not-a-real-token\n")

    monkeypatch.setattr(st, "SITES", {SLUG: {"title": "T", "repo": "o/t.github.io",
                                              "url": "https://t.example/"}})
    monkeypatch.setattr(st, "clone_dir", lambda slug: clone)
    monkeypatch.setattr(st, "token_file", lambda slug: token)
    # GitHub is the bare repository; on a live install the URL comes from github.json.
    monkeypatch.setattr(st, "remote_url", lambda slug: str(origin))
    monkeypatch.setattr(st, "POLL_S", 0)
    # The owner's click tokens, written as this test's user.
    monkeypatch.setattr(gate, "TOKEN_PATH", tmp_path / "card-tokens.json")
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    monkeypatch.delenv(gate.TOKEN_ENV, raising=False)

    served: dict = {"stale": False}

    def live(url: str):
        path = url.removeprefix("https://t.example/").split("?")[0]
        ref = "main~1" if served["stale"] else "main"
        r = subprocess.run(["git", "--git-dir", str(origin), "show", f"{ref}:{path}"],
                           capture_output=True)
        return r.stdout if r.returncode == 0 else None

    monkeypatch.setattr(st, "_live_bytes", live)
    return {"origin": origin, "seed": seed, "clone": clone, "served": served}


def _origin_log(origin: Path) -> list[str]:
    return _run("git", "--git-dir", str(origin), "log", "--format=%an|%s", "main").splitlines()


def _click(commit: str, slug: str = SLUG) -> tuple[dict, str]:
    """The owner's click: the bot previews the staged commit and mints the
    token for what the card showed. Returns (item, token)."""
    pv = st.cmd_preview(slug, commit, 20000)
    item = gate.item_of({"target": "site", "name": slug, "commit": commit}, pv)
    return item, gate.mint(item)


def _publish(message: str, wait: bool = False, slug: str = SLUG) -> dict:
    """The whole flow: the EA commits, the owner clicks, the bot publishes."""
    item, token = _click(st.cmd_commit(slug, message)["commit"], slug)
    return st.cmd_publish(slug, item["commit"], item["base"], wait, token=token)


def test_publish_commits_as_the_bot_account_pushes_and_waits_for_the_live_copy(env):
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    (env["clone"] / "agenda.html").write_text("<p>agenda</p>\n")
    res = _publish("Agenda page and a new index", wait=True)
    assert res["status"] == "published" and res["live"] is True
    assert sorted(res["files"]) == ["agenda.html", "index.html"]
    assert res["commit_url"].startswith("https://github.com/o/t.github.io/commit/")
    assert _origin_log(env["origin"])[0] == f"{BOT}|Agenda page and a new index"
    assert st.cmd_commit(SLUG, "again")["status"] == "unchanged"


def test_publish_reports_pushed_but_not_live_when_the_site_lags(env, monkeypatch):
    monkeypatch.setattr(st, "WAIT_S", 0)
    env["served"]["stale"] = True
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    res = _publish("v2", wait=True)
    assert res["status"] == "published" and res["live"] is False and "note" in res
    assert _origin_log(env["origin"])[0] == f"{BOT}|v2"


def test_publish_refuses_when_github_has_newer_commits_and_pull_fast_forwards(env):
    _owner_edit(env["seed"], "index.html", "<p>owner</p>\n")
    (env["clone"] / "notes.html").write_text("<p>ea</p>\n")
    with pytest.raises(st.SiteError, match="Run pull"):
        _publish("ea edit", wait=False)
    assert st.cmd_pull(SLUG)["status"] == "pulled"
    assert (env["clone"] / "index.html").read_text() == "<p>owner</p>\n"
    assert _publish("ea edit", wait=False)["status"] == "published"


def test_publish_never_forces_over_a_push_that_lands_meanwhile(env, monkeypatch):
    (env["clone"] / "index.html").write_text("<p>ea</p>\n")
    real_git = st._git

    def git(slug, *args):
        if args[0] == "push":
            _owner_edit(env["seed"], "other.html", "<p>owner</p>\n")
        return real_git(slug, *args)

    monkeypatch.setattr(st, "_git", git)
    with pytest.raises(st.SiteError, match="refused the push"):
        _publish("ea edit", wait=False)
    assert _origin_log(env["origin"])[0] == "owner|owner edits other.html"


def test_publish_refuses_an_em_dash_in_a_changed_page(env):
    (env["clone"] / "index.html").write_text("<p>one</p>\n<p>two — three</p>\n")
    with pytest.raises(st.SiteError, match="index.html:2"):
        _publish("dash", wait=False)
    (env["clone"] / "index.html").write_text("<p>two &mdash; three</p>\n")
    with pytest.raises(st.SiteError, match="em-dash"):
        _publish("dash", wait=False)
    assert len(_origin_log(env["origin"])) == 1


def test_publish_needs_a_message(env):
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    with pytest.raises(st.SiteError, match="--message"):
        _publish("  ", wait=False)


def test_revert_commits_the_undo_and_it_publishes_through_the_gate(env):
    (env["clone"] / "index.html").write_text("<p>bad</p>\n")
    sha = _publish("bad edit")["commit"]
    res = st.cmd_revert(SLUG, sha)
    assert res["status"] == "reverted"
    assert res["stage"]["request"] == {"site": SLUG, "commit": res["commit"]}
    assert _origin_log(env["origin"])[0] == f"{BOT}|bad edit"
    item, token = _click(res["commit"])
    assert st.cmd_publish(SLUG, item["commit"], item["base"], True, token=token)["live"] is True
    assert (env["clone"] / "index.html").read_text() == "<p>v1</p>\n"
    with pytest.raises(st.SiteError, match="not a published commit"):
        st.cmd_revert(SLUG, "0" * 40)
    with pytest.raises(st.SiteError, match="commit id"):
        st.cmd_revert(SLUG, "HEAD~1")


def test_status_and_diff_show_the_unpublished_change(env):
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    (env["clone"] / "new.html").write_text("<p>new</p>\n")
    status = st.cmd_status(SLUG)
    assert {c["path"]: c["change"] for c in status["changes"]} == {
        "index.html": "modified", "new.html": "added"}
    assert status["unpushed_commits"] == 0 and status["github_commits_missing"] == 0
    diff = st.cmd_diff(SLUG, 20000)
    assert "+<p>v2</p>" in diff["diff"] and diff["untracked"] == ["new.html"]


def test_a_hook_written_into_the_clone_never_runs(env, tmp_path):
    marker = tmp_path / "ran"
    hook = env["clone"] / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    assert _publish("v2", wait=False)["status"] == "published"
    assert not marker.exists()


def test_unknown_site_is_refused_and_list_names_the_sites(env, capsys):
    assert st.main(["status", "--site", "nope"]) == 2
    assert "known sites: t-site" in json.loads(capsys.readouterr().err)["error"]
    assert st.cmd_list()["sites"][0]["cloned"] is True


def test_clone_and_token_paths_come_from_passwd_not_home(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/not-ea-bot")
    slug = "a-site"
    monkeypatch.setattr(st, "SITES", {slug: {"title": "A", "repo": "o/a", "url": "https://a/"}})
    assert not str(st.clone_dir(slug)).startswith("/tmp/not-ea-bot")
    assert not str(st.token_file(slug)).startswith("/tmp/not-ea-bot")
    assert st.clone_dir(slug).parts[-3:] == ("presentations", "sites", slug)


def test_every_site_uses_the_one_bot_token(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_home", lambda: tmp_path)
    monkeypatch.setattr(st, "SITES", {"a": {"title": "A", "repo": "o/a", "url": "https://a/"},
                                      "b": {"title": "B", "repo": "o/b", "url": "https://b/",
                                            "token": "ignored"}})
    for slug in st.SITES:
        assert st.token_file(slug) == tmp_path / ".config" / BOT / "token"


def test_publish_without_a_token_keeps_the_commit_and_says_who_installs_it(env, monkeypatch, tmp_path):
    monkeypatch.setattr(st, "token_file", lambda slug: tmp_path / "missing")
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    with pytest.raises(st.SiteError, match="owner installs it"):
        _publish("v2", wait=False)
    assert _origin_log(env["origin"])[0] == "owner|init"
    assert st.cmd_status(SLUG)["unpushed_commits"] == 1


def test_an_em_dash_already_in_a_file_does_not_block_an_edit_to_another_line(env):
    _owner_edit(env["seed"], "schedule.yml", "a: one &mdash; two\nb: three\n")
    st.cmd_pull(SLUG)
    (env["clone"] / "schedule.yml").write_text("a: one &mdash; two\nb: three\nc: four\n")
    assert _publish("a line without a dash", wait=False)["status"] == "published"
    (env["clone"] / "schedule.yml").write_text("a: one &mdash; two\nb: three\nc: four\nd: x — y\n")
    with pytest.raises(st.SiteError, match=r"schedule.yml:4\b"):
        _publish("a line with a dash", wait=False)


def _jekyll(env, monkeypatch, conclusion="success"):
    site = {**st.SITES[SLUG], "build": ".github/workflows/pages.yml"}
    monkeypatch.setattr(st, "SITES", {SLUG: site})
    monkeypatch.setattr(st, "BUILD_POLL_S", 0)
    asked = []

    def api(url):
        asked.append(url)
        sha = url.split("head_sha=")[1].split("&")[0]
        return {"workflow_runs": [
            {"path": ".github/workflows/tests.yml", "status": "completed", "conclusion": "failure"},
            {"path": ".github/workflows/pages.yml", "status": "completed", "conclusion": conclusion,
             "head_sha": sha, "html_url": "https://github.com/o/t.github.io/actions/runs/1"}]}

    monkeypatch.setattr(st, "_api_json", api)
    return asked


def test_a_jekyll_site_waits_for_its_build_and_checks_only_the_files_jekyll_copies(env, monkeypatch):
    asked = _jekyll(env, monkeypatch)
    clone = env["clone"]
    (clone / "_config.yml").write_text("exclude:\n  - \"*.xlsx\"\n")
    (clone / "_data").mkdir()
    (clone / "_data" / "schedule.yml").write_text("- slides: x\n")
    (clone / "schedule.md").write_text("# Schedule\n")
    (clone / "page.html").write_text("---\nlayout: single\n---\n<p>x</p>\n")
    (clone / "plan.xlsx").write_bytes(b"PK\x03\x04")
    (clone / "slides").mkdir()
    (clone / "slides" / "L14-x.pdf").write_bytes(b"%PDF-1.7\n\xff\xfe\n")
    res = _publish("L14 slides", wait=True)
    assert res["live"] is True and res["checked"] == ["slides/L14-x.pdf"]
    assert res["build_url"].endswith("/actions/runs/1")
    assert "head_sha=" in asked[0] and "event=push" in asked[0]


def test_a_failed_build_is_reported_as_not_live(env, monkeypatch):
    _jekyll(env, monkeypatch, conclusion="failure")
    (env["clone"] / "index.html").write_text("<p>v2</p>\n")
    res = _publish("v2", wait=True)
    assert res["status"] == "published" and res["live"] is False
    assert "build failed" in res["note"] and res["build_url"]


def test_add_file_copies_a_deck_pdf_into_the_clone_and_nothing_else(env, monkeypatch, tmp_path):
    decks, secret = tmp_path / "decks", tmp_path / "config"
    (decks / "lec14" / "public").mkdir(parents=True)
    secret.mkdir()
    pdf = decks / "lec14" / "public" / "lec14.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    (secret / "token.pdf").write_bytes(b"%PDF-1.7\n")
    (decks / "lec14" / "notes.txt").write_text("x")
    (decks / "lec14" / "leak.pdf").symlink_to(secret / "token.pdf")
    monkeypatch.setattr(st, "file_roots", lambda: (decks,))
    res = st.cmd_add_file(SLUG, str(pdf), "slides/L14-quantization.pdf")
    assert res["status"] == "added"
    assert (env["clone"] / "slides" / "L14-quantization.pdf").read_bytes() == b"%PDF-1.7\n"
    assert st.cmd_add_file(SLUG, str(pdf), "slides/L14-quantization.pdf")["status"] == "replaced"
    for src, dest, why in [
            (secret / "token.pdf", "slides/a.pdf", "deck folders"),
            (decks / "lec14" / "leak.pdf", "slides/a.pdf", "deck folders"),
            (decks / "lec14" / "notes.txt", "slides/a.txt", "PDF or an image"),
            (pdf, "../outside.pdf", "inside the site"),
            (pdf, ".github/x.pdf", "inside the site"),
            (pdf, "/tmp/x.pdf", "inside the site"),
            (pdf, "slides/a.html", "file type")]:
        with pytest.raises(st.SiteError, match=why):
            st.cmd_add_file(SLUG, str(src), dest)
    (env["clone"] / "out").symlink_to(tmp_path)
    with pytest.raises(st.SiteError, match="outside the clone"):
        st.cmd_add_file(SLUG, str(pdf), "out/x.pdf")


def test_the_sites_and_the_bot_account_come_from_github_json(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "github.json").write_text(json.dumps({
        "_doc": {"sites": "ignored"}, "bot_login": "acme-bot", "bot_email": "a@b",
        "bot_token_dir": "acme-bot",
        "sites": {"_example": {"title": "ignored"},
                  "course": {"title": "Course", "repo": "org/course.github.io",
                             "url": "https://course.github.io/", "build": ".github/workflows/pages.yml"},
                  "status": {"title": "Status", "repo": "org/status", "url": "https://x/",
                             "private": True, "deploy_note": "pulled every 5 minutes"}}}))
    monkeypatch.setattr(st, "EA_DIR", tmp_path)
    cfg = st._github_config()
    assert sorted(cfg["sites"]) == ["course", "status"]
    assert cfg["sites"]["course"]["build"] == ".github/workflows/pages.yml"
    assert cfg["sites"]["status"]["private"] is True
    assert (cfg["bot_login"], cfg["bot_email"], cfg["bot_token_dir"]) == ("acme-bot", "a@b", "acme-bot")
    assert cfg["doc_review"] == {"repo": "", "owner_login": ""}
    monkeypatch.setattr(st, "EA_DIR", tmp_path / "nowhere")
    assert st._github_config()["sites"] == {}


def test_an_unconfigured_token_dir_is_refused_in_one_line(monkeypatch):
    monkeypatch.setattr(st, "SITES", {SLUG: {"title": "T", "repo": "o/t", "url": "https://t/"}})
    monkeypatch.setattr(st, "BOT_TOKEN_DIR", "")
    with pytest.raises(st.SiteError, match="github.json names no bot_token_dir; see docs/SETUP.md"):
        st.token_file(SLUG)


def test_an_unconfigured_install_is_refused_in_one_line(env, monkeypatch):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(st, "SITES", {})
        with pytest.raises(st.SiteError, match="github.json names no sites; see docs/SETUP.md"):
            st.cmd_status(SLUG)
        assert st.cmd_list()["sites"] == []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(st, "BOT_NAME", "")
        (env["clone"] / "index.html").write_text("<p>v2</p>\n")
        with pytest.raises(st.SiteError, match="github.json names no bot_login or bot_email; see docs/SETUP.md"):
            st.cmd_commit(SLUG, "v2")


# --- the owner's click (publish_gate) ----------------------------------------

def _staged(env, text="<p>v2</p>\n") -> dict:
    (env["clone"] / "index.html").write_text(text)
    commit = st.cmd_commit(SLUG, "v2")["commit"]
    pv = st.cmd_preview(SLUG, commit, 20000)
    return gate.item_of({"target": "site", "name": SLUG, "commit": commit}, pv)


def test_publish_without_the_owners_token_is_refused_and_pushes_nothing(env, monkeypatch, capsys):
    item = _staged(env)
    with pytest.raises(st.SiteError, match="BLOCKED: this verb publishes outside the host"):
        st.cmd_publish(SLUG, item["commit"], item["base"], False)
    with pytest.raises(st.SiteError, match="BLOCKED: invalid, expired or already used"):
        st.cmd_publish(SLUG, item["commit"], item["base"], False, token="made-up")
    monkeypatch.setenv(gate.TOKEN_ENV, "made-up-too")
    assert st.main(["publish", "--site", SLUG, "--commit", item["commit"],
                    "--base", item["base"], "--no-wait"]) == 2
    assert "BLOCKED" in json.loads(capsys.readouterr().err)["error"]
    # the old one-step form is gone: a message is no commit
    with pytest.raises(SystemExit):
        st.main(["publish", "--site", SLUG, "--message", "v2"])
    assert _origin_log(env["origin"]) == ["owner|init"]


def test_publish_takes_the_token_from_the_environment_as_the_bot_passes_it(env, monkeypatch):
    item = _staged(env)
    monkeypatch.setenv(gate.TOKEN_ENV, gate.mint(item))
    assert st.main(["publish", "--site", SLUG, "--commit", item["commit"],
                    "--base", item["base"], "--no-wait"]) == 0
    assert _origin_log(env["origin"])[0] == f"{BOT}|v2"


def test_a_token_for_one_commit_does_not_publish_another(env):
    first = _staged(env)
    token = gate.mint(first)
    (env["clone"] / "extra.html").write_text("<p>not reviewed</p>\n")
    second = st.cmd_commit(SLUG, "extra")["commit"]
    with pytest.raises(st.SiteError, match="BLOCKED: invalid"):
        st.cmd_publish(SLUG, second, first["base"], False, token=token)
    # the refusal did not spend the token, and the reviewed commit is what goes
    assert st.cmd_publish(SLUG, first["commit"], first["base"], False, token=token)["status"] == "published"
    head = _run("git", "--git-dir", str(env["origin"]), "rev-parse", "main").strip()
    assert head == first["commit"]
    shown = _run("git", "--git-dir", str(env["origin"]), "ls-tree", "--name-only", "main")
    assert "extra.html" not in shown


def test_a_token_works_once(env):
    item = _staged(env)
    token = gate.mint(item)
    assert st.cmd_publish(SLUG, item["commit"], item["base"], False, token=token)["status"] == "published"
    # GitHub's main is put back, so the same commit is publishable again
    _run("git", "--git-dir", str(env["origin"]), "update-ref", "refs/heads/main", item["base"])
    with pytest.raises(st.SiteError, match="BLOCKED: invalid, expired or already used"):
        st.cmd_publish(SLUG, item["commit"], item["base"], False, token=token)


def test_publish_refuses_when_github_moved_after_the_card(env):
    item = _staged(env)
    token = gate.mint(item)
    _owner_edit(env["seed"], "other.html", "<p>owner</p>\n")
    with pytest.raises(st.SiteError, match="Run pull"):
        st.cmd_publish(SLUG, item["commit"], item["base"], False, token=token)
    assert _origin_log(env["origin"])[0] == "owner|owner edits other.html"
    with pytest.raises(st.SiteError, match="full --commit and --base"):
        st.cmd_publish(SLUG, item["commit"][:7], item["base"], False, token=token)


def test_preview_names_the_files_and_the_diff_and_refuses_what_github_has(env):
    (env["clone"] / "index.html").write_text("<p>v2 kit.calloway@example.edu</p>\n")
    (env["clone"] / "new.html").write_text("<p>new</p>\n")
    commit = st.cmd_commit(SLUG, "two files")["commit"]
    (env["clone"] / "later.html").write_text("<p>not committed</p>\n")
    pv = st.cmd_preview(SLUG, commit[:7], 20000)
    assert pv["commit"] == commit and pv["subject"] == "two files" and pv["commits"] == 1
    assert {f["path"]: f["change"] for f in pv["files"]} == {"index.html": "modified",
                                                              "new.html": "added"}
    assert "+<p>v2 kit.calloway@example.edu</p>" in pv["diff"]
    assert gate.scan_diff(pv["diff"])["counts"] == {"email address": 1}
    base = pv["base"]
    with pytest.raises(st.SiteError, match="nothing to publish"):
        st.cmd_preview(SLUG, base, 20000)
    with pytest.raises(st.SiteError, match="not a commit"):
        st.cmd_preview(SLUG, "1234567", 20000)


def test_a_token_is_bound_to_the_repository_the_card_named(env, monkeypatch):
    """github.json is ea-bot space: re-pointing the slug at another
    repository after the card makes the click's token fail."""
    (env["clone"] / "index.html").write_text("<p>v3</p>\n")
    item, token = _click(st.cmd_commit(SLUG, "v3")["commit"])
    assert item["repo"] == st.SITES[SLUG]["repo"]
    other = {**st.SITES[SLUG], "repo": "o/elsewhere"}
    monkeypatch.setitem(st.SITES, SLUG, other)
    with pytest.raises(st.SiteError, match="BLOCKED"):
        st.cmd_publish(SLUG, item["commit"], item["base"], False, token=token)
