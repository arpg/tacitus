"""The developer guide reaches sessions that change the code and never the
EA. Claude Code loads both CLAUDE.md and .claude/CLAUDE.md from its working
directory, so every copy the EA runs from leaves the guide out: the deploys
into ea-bot's checkout keep it out with a sparse checkout, git archive (the
/opt tree) skips it by export-ignore, and the Docker context ignores it."""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / ".claude" / "CLAUDE.md"
SPARSE = "sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md'"
DEPLOYS = (".githooks/post-commit", "scripts/deploy.sh", "deploy/macos/deploy.sh",
           "scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh")


def _in_image() -> bool:
    # The Docker image carries the tree without .git and without the guide;
    # the script-style runner counts a pytest skip as a failure.
    return not (REPO / ".git").exists() and not GUIDE.exists()


def test_the_guide_says_what_the_root_file_is_and_the_ci_rule():
    if _in_image():
        print("  (no developer guide in the image, as intended)")
        return
    text = GUIDE.read_text()
    assert "The root `CLAUDE.md` is the EA's operating file" in text
    assert "`full-ci`" in text and "tests/run_wall.py" in text
    assert "No real names or mail content in public text" in text
    assert chr(0x2014) not in text


def test_every_copy_the_ea_runs_from_leaves_the_guide_out():
    for rel in DEPLOYS:
        text = (REPO / rel).read_text()
        assert SPARSE in text, rel
        # after the copy is made or reset, so a first clone loses it too
        anchor = "reset" if "reset" in text[:text.index(SPARSE)] else "clone"
        assert text.index(anchor) < text.index(SPARSE), rel
    assert ".claude/CLAUDE.md" in (REPO / ".dockerignore").read_text().splitlines()
    if not (REPO / ".git").exists():
        return
    out = subprocess.run(["git", "-C", str(REPO), "check-attr", "export-ignore", ".claude/CLAUDE.md"],
                         capture_output=True, text=True).stdout
    assert out.strip().endswith("export-ignore: set"), out


def test_a_sparse_checkout_keeps_the_guide_out_across_a_reset(tmp_path):
    def git(cwd, *args):
        return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                              cwd=cwd, capture_output=True, text=True, check=True).stdout
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / ".claude" / "agents").mkdir(parents=True)
    git(src, "init", "-q")
    (src / "CLAUDE.md").write_text("core\n")
    (src / ".claude" / "CLAUDE.md").write_text("guide\n")
    (src / ".claude" / "agents" / "email-author.md").write_text("agent\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "one")
    git(tmp_path, "clone", "-q", str(src), str(dst))
    git(dst, *SPARSE.replace("'", "").split())
    (src / ".claude" / "CLAUDE.md").write_text("guide, changed\n")
    git(src, "commit", "-qam", "two")
    git(dst, "fetch", "-q", str(src), "HEAD")
    git(dst, "reset", "-q", "--hard", "FETCH_HEAD")
    assert not (dst / ".claude" / "CLAUDE.md").exists()
    assert (dst / "CLAUDE.md").exists() and (dst / ".claude" / "agents" / "email-author.md").exists()
    assert git(dst, "status", "--short") == ""
