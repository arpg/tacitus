"""The EA's Overleaf projects (overleaf_tools.py).

What must hold: commit commits every change as the bot account
(github.json); push runs only with the owner's one-time token for exactly
that commit on top of the Overleaf head the card showed, and never forces;
when Overleaf has newer commits, pull merges them, and a conflict leaves the
clone as it was and names the files unless --prefer picks a side; commit,
preview and push refuse an em-dash on a line the EA added; fetch and push
go to the URL built from the project id; a clone whose .git/config
holds an unsafe key is refused, and neither a hook in the clone nor a
filter in ~/.gitconfig ever runs; every clone lives under the roots.

git runs for real against a local bare repository standing in for
Overleaf.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import overleaf_tools as ot  # noqa: E402
import publish_gate as gate  # noqa: E402
import resident_io  # noqa: E402

PID = "0a1b2c3d4e5f60718293a4b5"
# A live clone's origin names `git` as the URL user, the form Overleaf hands out.
GIT_USER_REMOTE = ot.OVERLEAF_GIT.replace("://", "://git@")
BOT = "t-bot"
# The bot account comes from github.json on a live install; the tests pin their own.
ot.BOT_NAME = BOT
ot.BOT_EMAIL = "1+t-bot@users.noreply.github.com"


def _run(*cmd, cwd=None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def _owner_edit(seed: Path, name: str, text: str) -> None:
    """The owner edits the project on Overleaf."""
    _run("git", "pull", "-q", "--ff-only", cwd=seed)
    (seed / name).write_text(text)
    _run("git", "add", name, cwd=seed)
    _run("git", "commit", "-q", "-m", "Update on Overleaf.", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bare 'Overleaf' project, the owner's working copy of it, and ea-bot's
    clone under a clone root with the canonical Overleaf origin."""
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    docs = tmp_path / "docs"
    clone = docs / "proj" / "overleaf"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    _run("git", "clone", "-q", str(origin), str(seed))
    for c in (["config", "user.name", "kit.calloway"], ["config", "user.email", "c@c"]):
        _run("git", *c, cwd=seed)
    (seed / "main.tex").write_text("% header\n\\title{Snow}\nBody one.\nBody two.\n")
    (seed / "refs.bib").write_text("@misc{a}\n")
    _run("git", "add", "-A", cwd=seed)
    _run("git", "commit", "-q", "-m", "init", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)
    _run("git", "clone", "-q", str(origin), str(clone))
    _run("git", "config", "remote.origin.url", f"{GIT_USER_REMOTE}/{PID}", cwd=clone)
    _run("git", "config", "user.name", "Kit Calloway", cwd=clone)
    token = tmp_path / "token"
    token.write_text("not-a-real-token\n")
    monkeypatch.setattr(ot, "roots", lambda: [docs])
    monkeypatch.setattr(ot, "remote_url", lambda project: str(origin))
    monkeypatch.setattr(ot, "token_file", lambda: token)
    # The owner's click tokens, written as this test's user.
    monkeypatch.setattr(gate, "TOKEN_PATH", tmp_path / "card-tokens.json")
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    monkeypatch.delenv(gate.TOKEN_ENV, raising=False)
    return {"origin": origin, "seed": seed, "clone": clone, "docs": docs, "tmp": tmp_path}


def _origin(origin: Path, *args: str) -> str:
    return _run("git", "--git-dir", str(origin), *args)


def _origin_log(origin: Path) -> list[str]:
    return _origin(origin, "log", "--format=%an|%s", "main").splitlines()


def _head(clone: Path) -> str:
    return _run("git", "rev-parse", "HEAD", cwd=clone).strip()


def _click(clone: Path) -> tuple[dict, str]:
    """The owner's click: the bot previews the clone's head and mints the
    token for what the card showed. Returns (item, token)."""
    pv = ot.cmd_preview(str(clone), _head(clone), 20000)
    item = gate.item_of({"target": "overleaf", "name": str(clone)}, pv)
    return item, gate.mint(item)


def _push(clone: Path, message: str | None = None) -> dict:
    """The whole flow: the EA commits, the owner clicks, the bot pushes."""
    if message:
        ot.cmd_commit(str(clone), message)
    item, token = _click(clone)
    return ot.cmd_push(str(clone), item["commit"], item["base"], token=token)


def test_push_commits_as_the_bot_account_and_status_counts(env):
    clone = env["clone"]
    (clone / "main.tex").write_text("% header\n\\title{Clothing}\nBody one.\nBody two.\n")
    st = ot.cmd_status(str(clone))
    assert st["project"] == PID and st["branch"] == "main"
    assert st["unpushed_commits"] == 0 and st["overleaf_commits_missing"] == 0
    assert st["changes"] == [{"path": "main.tex", "change": "modified"}]
    res = _push(clone, "Retitle")
    assert res["status"] == "pushed" and res["files"] == ["main.tex"]
    assert res["project_url"] == f"https://www.overleaf.com/project/{PID}"
    # the clone's own user.name is the owner's; the commit is still the bot account's
    assert _origin_log(env["origin"])[0] == f"{BOT}|Retitle"
    with pytest.raises(ot.OverleafError, match="nothing to push"):
        ot.cmd_preview(str(clone), _head(clone), 20000)


def test_an_unconfigured_bot_account_is_refused_in_one_line(env, monkeypatch):
    monkeypatch.setattr(ot, "BOT_EMAIL", "")
    (env["clone"] / "main.tex").write_text("changed\n")
    with pytest.raises(ot.OverleafError, match="github.json names no bot_login or bot_email; see docs/SETUP.md"):
        ot.cmd_commit(str(env["clone"]), "Retitle")


def test_the_bot_account_comes_from_github_json(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "github.json").write_text(
        '{"_doc": "ignored", "bot_login": "acme-bot", "bot_email": "9+acme-bot@users.noreply.github.com"}')
    monkeypatch.setattr(ot, "EA_DIR", tmp_path)
    cfg = ot._github_config()
    assert (cfg["bot_login"], cfg["bot_email"]) == ("acme-bot", "9+acme-bot@users.noreply.github.com")
    assert cfg["bot_token_dir"] == "" and cfg["sites"] == {}
    monkeypatch.setattr(ot, "EA_DIR", tmp_path / "nowhere")
    assert ot._github_config() == ot.GITHUB_DEFAULTS


def test_commit_needs_a_message_and_push_sends_only_the_commit(env):
    clone = env["clone"]
    (clone / "main.tex").write_text("changed\n")
    with pytest.raises(ot.OverleafError, match="--message"):
        ot.cmd_commit(str(clone), "  ")
    ot.cmd_commit(str(clone), "Change")
    (clone / "refs.bib").write_text("@misc{uncommitted}\n")
    assert _push(clone)["files"] == ["main.tex"]
    assert _origin(env["origin"], "show", "main:refs.bib") == "@misc{a}\n"


def test_pull_merges_an_overleaf_edit_to_other_lines_then_push_sends_the_merge(env):
    clone = env["clone"]
    _owner_edit(env["seed"], "refs.bib", "@misc{a}\n@misc{b}\n")
    (clone / "main.tex").write_text("% header\n\\title{Clothing}\nBody one.\nBody two.\n")
    ot.cmd_commit(str(clone), "Retitle")
    with pytest.raises(ot.OverleafError, match="Run pull"):
        ot.cmd_preview(str(clone), _head(clone), 20000)
    pulled = ot.cmd_pull(str(clone), None)
    assert pulled["overleaf_commits"] == 1 and pulled["stage"]["request"]["commit"] == _head(clone)
    res = _push(clone)
    assert res["status"] == "pushed"
    assert _origin(env["origin"], "show", "main:refs.bib") == "@misc{a}\n@misc{b}\n"
    assert "Clothing" in _origin(env["origin"], "show", "main:main.tex")


def test_a_conflict_leaves_the_clone_as_it_was_and_prefer_resolves_it(env):
    clone = env["clone"]
    # the stray keystroke on Overleaf, on the line the EA rewrote
    _owner_edit(env["seed"], "main.tex", "c% header\n\\title{Snow}\nBody one.\nBody two.\n")
    (clone / "main.tex").write_text("% new header\n\\title{Clothing}\nBody one.\nBody two.\n")
    before = _origin(env["origin"], "rev-parse", "main")
    ot.cmd_commit(str(clone), "Retitle")
    with pytest.raises(ot.OverleafError, match=r"same lines in main\.tex.*--prefer"):
        ot.cmd_pull(str(clone), None)
    assert _origin(env["origin"], "rev-parse", "main") == before
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert ot.cmd_status(str(clone))["unpushed_commits"] == 1
    assert ot.cmd_status(str(clone))["changes"] == []
    pulled = ot.cmd_pull(str(clone), "local")
    assert pulled["status"] == "pulled" and pulled["merged_with_local_commits"] is True
    assert (clone / "main.tex").read_text().startswith("% new header\n")
    assert _push(clone)["status"] == "pushed"
    assert _origin(env["origin"], "show", "main:main.tex").startswith("% new header\n")


def test_prefer_overleaf_keeps_the_overleaf_lines(env):
    clone = env["clone"]
    _owner_edit(env["seed"], "main.tex", "% header\n\\title{Owner}\nBody one.\nBody two.\n")
    (clone / "main.tex").write_text("% header\n\\title{EA}\nBody one.\nBody two.\n")
    ot.cmd_commit(str(clone), "EA title")
    with pytest.raises(ot.OverleafError, match="same lines"):
        ot.cmd_pull(str(clone), None)
    ot.cmd_pull(str(clone), "overleaf")
    assert "\\title{Owner}" in (clone / "main.tex").read_text()


def test_pull_fast_forwards_and_refuses_uncommitted_changes(env):
    clone = env["clone"]
    _owner_edit(env["seed"], "refs.bib", "@misc{z}\n")
    (clone / "main.tex").write_text("local edit\n")
    with pytest.raises(ot.OverleafError, match="uncommitted"):
        ot.cmd_pull(str(clone), None)
    _run("git", "checkout", "--", "main.tex", cwd=clone)
    res = ot.cmd_pull(str(clone), None)
    assert res["status"] == "pulled" and res["merged_with_local_commits"] is False
    assert (clone / "refs.bib").read_text() == "@misc{z}\n"
    assert ot.cmd_pull(str(clone), None)["status"] == "unchanged"


def test_diff_shows_what_push_sends_and_what_overleaf_has(env):
    clone = env["clone"]
    (clone / "main.tex").write_text("% header\n\\title{Committed}\nBody one.\nBody two.\n")
    ot.cmd_commit(str(clone), "first")
    (clone / "notes.tex").write_text("new file\n")
    (clone / "refs.bib").write_text("@misc{local}\n")
    local = ot.cmd_diff(str(clone), False, 20000)
    assert "+\\title{Committed}" in local["diff"] and "+@misc{local}" in local["diff"]
    assert local["untracked"] == ["notes.tex"]
    _owner_edit(env["seed"], "main.tex", "% header\n\\title{Snow}\nBody one.\nOwner line.\n")
    remote = ot.cmd_diff(str(clone), True, 20000)
    assert "+Owner line." in remote["diff"] and "Committed" not in remote["diff"]


@pytest.mark.parametrize("line,refused", [
    ("A pause—then more.", True),
    ("A pause---then more.", True),
    ("Pages 3--5, an en-dash range.", False),
    ("%---------- section rule", False),
    ("Text % a comment --- with a dash", False),
    ("50\\% of it---and more.", True),
])
def test_commit_refuses_an_em_dash_on_an_added_line(env, line, refused):
    clone = env["clone"]
    (clone / "main.tex").write_text(f"% header\n\\title{{Snow}}\n{line}\nBody two.\n")
    if refused:
        with pytest.raises(ot.OverleafError, match=r"em-dash on an added line in main\.tex:3"):
            ot.cmd_commit(str(clone), "dash")
    else:
        assert ot.cmd_commit(str(clone), "dash")["status"] == "committed"


def test_push_refuses_an_em_dash_in_an_earlier_local_commit(env):
    clone = env["clone"]
    (clone / "main.tex").write_text("% header\n\\title{Snow---Ice}\nBody one.\nBody two.\n")
    _run("git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "outside the tool",
         cwd=clone)
    with pytest.raises(ot.OverleafError, match="em-dash"):
        ot.cmd_preview(str(clone), _head(clone), 20000)
    base = _origin(env["origin"], "rev-parse", "main").strip()
    item = {"target": "overleaf", "name": str(clone), "commit": _head(clone), "base": base,
            "project": PID}
    with pytest.raises(ot.OverleafError, match="em-dash"):
        ot.cmd_push(str(clone), item["commit"], base, token=gate.mint(item))
    assert len(_origin_log(env["origin"])) == 1


def test_a_hook_or_a_global_filter_never_runs(env, monkeypatch):
    clone, tmp = env["clone"], env["tmp"]
    marker = tmp / "ran"
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    home = tmp / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[filter \"evil\"]\n\tclean = touch {marker}; cat\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    (clone / ".gitattributes").write_text("*.tex filter=evil\n")
    (clone / "main.tex").write_text("changed\n")
    _push(clone, "edit")
    assert not marker.exists()


@pytest.mark.parametrize("key,value", [
    ("filter.evil.clean", "touch /tmp/x"),
    ("core.sshCommand", "touch /tmp/x"),
    ("include.path", "/tmp/other"),
    ("url.https://evil.example/.insteadOf", "https://git.overleaf.com/"),
    ("http.proxy", "http://evil.example:8080"),
])
def test_an_unsafe_key_in_the_clone_config_is_refused(env, key, value):
    _run("git", "config", key, value, cwd=env["clone"])
    with pytest.raises(ot.OverleafError, match="does not run with"):
        ot.cmd_status(str(env["clone"]))


def test_a_clone_whose_origin_is_not_overleaf_is_refused(env):
    _run("git", "config", "remote.origin.url", "https://evil.example/repo", cwd=env["clone"])
    with pytest.raises(ot.OverleafError, match="not an Overleaf clone"):
        ot.cmd_status(str(env["clone"]))


def test_dirs_outside_the_roots_are_refused(env, tmp_path):
    with pytest.raises(ot.OverleafError, match="outside the clone roots"):
        ot.cmd_status(str(tmp_path / "seed"))
    with pytest.raises(ot.OverleafError, match="absolute"):
        ot.cmd_status("docs/proj/overleaf")
    link = env["docs"] / "escape"
    link.symlink_to(tmp_path / "seed")
    with pytest.raises(ot.OverleafError, match="outside the clone roots"):
        ot.cmd_status(str(link))


def test_clone_lands_under_a_root_with_the_canonical_origin(env):
    dest = env["docs"] / "other" / "overleaf"
    res = ot.cmd_clone(f"https://www.overleaf.com/project/{PID}", str(dest))
    assert res["status"] == "cloned" and res["branch"] == "main" and res["files"] == 2
    assert _run("git", "config", "remote.origin.url", cwd=dest).strip() == \
        f"https://git.overleaf.com/{PID}"
    assert ot.cmd_status(str(dest))["project"] == PID
    with pytest.raises(ot.OverleafError, match="already exists"):
        ot.cmd_clone(PID, str(dest))


def test_list_names_the_overleaf_clones(env):
    github = env["docs"] / "site"
    _run("git", "init", "-q", str(github))
    _run("git", "config", "remote.origin.url", "https://github.com/o/r", cwd=github)
    _run("git", "config", "credential.helper", "store", cwd=github)
    clones = ot.cmd_list()["clones"]
    assert clones == [{"dir": str(env["clone"]), "project": PID,
                       "project_url": f"https://www.overleaf.com/project/{PID}"}]


@pytest.mark.parametrize("text,want", [
    (PID, PID),
    (f"https://www.overleaf.com/project/{PID}", PID),
    (f"https://git.overleaf.com/{PID}", PID),
    (f"{GIT_USER_REMOTE}/{PID}", PID),
    ("https://evil.example/0a1b2c3d4e5f60718293a4b5", None),
    ("0a1b2c3d4e5f60718293a4b5 --upload-pack=x", None),
])
def test_parse_project(text, want):
    if want:
        assert ot.parse_project(text) == want
    else:
        with pytest.raises(ot.OverleafError):
            ot.parse_project(text)


def test_without_a_token_the_error_says_how_to_install_one(env, monkeypatch):
    monkeypatch.setattr(ot, "token_file", lambda: env["tmp"] / "missing")
    with pytest.raises(ot.OverleafError, match="install-overleaf-token"):
        ot.cmd_status(str(env["clone"]))


def test_overleaf_auth_answers_are_explained():
    assert "not a member" in ot._explain("fatal: Authentication failed for 'https://git...'")
    assert "token" in ot._explain("remote: Enter your Git authentication token")


# --- the owner's click (publish_gate) ----------------------------------------

def _staged(env) -> dict:
    clone = env["clone"]
    (clone / "main.tex").write_text("% header\n\\title{Clothing}\nBody one.\nBody two.\n")
    ot.cmd_commit(str(clone), "Retitle")
    pv = ot.cmd_preview(str(clone), _head(clone)[:7], 20000)
    assert pv["files"] == [{"path": "main.tex", "change": "modified"}]
    assert pv["subject"] == "Retitle" and "+\\title{Clothing}" in pv["diff"]
    return gate.item_of({"target": "overleaf", "name": str(clone)}, pv)


def test_push_without_the_owners_token_is_refused_and_pushes_nothing(env, monkeypatch, capsys):
    item = _staged(env)
    with pytest.raises(ot.OverleafError, match="BLOCKED: this verb publishes outside the host"):
        ot.cmd_push(item["name"], item["commit"], item["base"])
    with pytest.raises(ot.OverleafError, match="BLOCKED: invalid"):
        ot.cmd_push(item["name"], item["commit"], item["base"], token="made-up")
    monkeypatch.setenv(gate.TOKEN_ENV, "made-up-too")
    assert ot.main(["push", "--dir", item["name"], "--commit", item["commit"],
                    "--base", item["base"], "--project", PID]) == 2
    assert "BLOCKED" in json.loads(capsys.readouterr().err)["error"]
    with pytest.raises(SystemExit):
        ot.main(["push", "--dir", item["name"], "--message", "Retitle"])
    assert _origin_log(env["origin"]) == ["kit.calloway|init"]


def test_a_token_for_one_commit_does_not_push_another(env):
    item = _staged(env)
    token = gate.mint(item)
    clone = env["clone"]
    (clone / "refs.bib").write_text("@misc{a}\n@misc{unreviewed}\n")
    ot.cmd_commit(str(clone), "Unreviewed")
    with pytest.raises(ot.OverleafError, match="BLOCKED: invalid"):
        ot.cmd_push(str(clone), _head(clone), item["base"], token=token)
    assert ot.cmd_push(str(clone), item["commit"], item["base"], token=token)["status"] == "pushed"
    assert _origin(env["origin"], "rev-parse", "main").strip() == item["commit"]
    assert _origin(env["origin"], "show", "main:refs.bib") == "@misc{a}\n"


def test_a_push_token_works_once(env):
    item = _staged(env)
    token = gate.mint(item)
    assert ot.cmd_push(item["name"], item["commit"], item["base"], token=token)["status"] == "pushed"
    _origin(env["origin"], "update-ref", "refs/heads/main", item["base"])
    with pytest.raises(ot.OverleafError, match="BLOCKED: invalid, expired or already used"):
        ot.cmd_push(item["name"], item["commit"], item["base"], token=token)


def test_push_refuses_when_overleaf_moved_after_the_card(env):
    item = _staged(env)
    token = gate.mint(item)
    _owner_edit(env["seed"], "refs.bib", "@misc{a}\n@misc{b}\n")
    with pytest.raises(ot.OverleafError, match="Run pull"):
        ot.cmd_push(item["name"], item["commit"], item["base"], token=token)
    assert _origin_log(env["origin"])[0] == "kit.calloway|Update on Overleaf."


OTHER_PID = "ffeeddccbbaa998877665544"


def test_the_push_token_binds_the_project_id(env):
    """A clone whose origin names another project after the card does not
    push on that card's token, and the bot's --project must match."""
    item = _staged(env)
    assert item["project"] == PID
    token = gate.mint(item)
    _run("git", "config", "remote.origin.url", f"{GIT_USER_REMOTE}/{OTHER_PID}", cwd=env["clone"])
    with pytest.raises(ot.OverleafError, match="BLOCKED: invalid"):
        ot.cmd_push(item["name"], item["commit"], item["base"], token=token)
    with pytest.raises(ot.OverleafError, match="not the " + PID):
        ot.cmd_push(item["name"], item["commit"], item["base"], token=token, project=PID)
    assert _origin_log(env["origin"]) == ["kit.calloway|init"]
    _run("git", "config", "remote.origin.url", f"{GIT_USER_REMOTE}/{PID}", cwd=env["clone"])
    res = ot.cmd_push(item["name"], item["commit"], item["base"], token=token, project=PID)
    assert res["status"] == "pushed"


def test_the_gate_names_the_project_on_the_push_it_runs():
    pv = {"commit": "a" * 40, "base": "b" * 40, "dir": "/d/overleaf", "project": PID}
    item = gate.item_of({"target": "overleaf", "name": "/d/overleaf"}, pv)
    assert item == {"target": "overleaf", "name": "/d/overleaf", "commit": "a" * 40,
                    "base": "b" * 40, "project": PID}
    with pytest.raises(gate.GateError, match="project"):
        gate.item_of({"target": "overleaf", "name": "/d"}, {**pv, "project": "x"})
    seen = []

    def runner(argv, timeout):
        seen.append(argv)
        return 0, '{"status": "pushed"}', ""

    gate.execute(item, "t", tree=Path("/x"), ea_dir="/e", home=Path("/h"), runner=runner)
    assert seen[0][seen[0].index("--project") + 1] == PID
