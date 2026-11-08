"""The install modes are reviewer, assistant and collaborator. No doc and no
script names the old profiles (`review-only`, and `email` as a profile)
any more; the code that maps an old name to its mode, the doctor and the
installers that accept one with a note, the mode section of docs/SETUP.md
and the CHANGELOG are the places that may."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OLD = re.compile(
    r"review-only"
    r"|--profile[ =]email\b"
    r"|EA_(?:SMOKE_)?PROFILE(?::-|=)email\b"
    r"|\bPROFILE=email\b"
    r"|[\"']profile[\"']\s*:\s*[\"']email[\"']"
    r"|\bemail\s+profiles?\b"
    r"|\bprofiles?\s+`?email`?(?![@\w.-])"
    r"|`email`\s*(?:,|or|and)\s*`?(?:review|assistant)")

# Where an old name may stand: each maps it to its mode, or says it did.
ALLOWED = {
    "CHANGELOG.md": "the release notes name what changed",
    "preprocessor/ea_config.py": "the mapping itself (LEGACY_PROFILES)",
    "scripts/ea-doctor.py": "--profile accepts an old name with a note",
    "scripts/install/setup.sh": "--profile accepts an old name with a note",
    "install.sh": "--profile accepts an old name with a note",
    "docs/SETUP.md": "the mode section says what each old name became",
}
SUFFIXES = {".md", ".py", ".sh", ".html", ".js", ".json", ".yml", ".yaml", ".txt", ".toml", ".css"}


def _files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                             text=True, check=True).stdout.split()
        paths = [ROOT / p for p in out]
    except (OSError, subprocess.CalledProcessError):  # the Docker image has no .git
        paths = [p for p in ROOT.rglob("*") if ".git" not in p.parts]
    keep = []
    for p in paths:
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix not in SUFFIXES or not p.is_file():
            continue
        # tests may exercise the mapping; tests/install holds scripts and answers files
        if rel.startswith("tests/") and not rel.startswith("tests/install/"):
            continue
        keep.append(p)
    return keep


def test_no_doc_or_script_names_an_old_profile():
    hits = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if OLD.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, "old profile names:\n" + "\n".join(hits)


def test_the_allowed_files_still_need_their_place():
    """An allowance that no longer matches anything is dropped."""
    for rel, why in ALLOWED.items():
        text = (ROOT / rel).read_text()
        assert OLD.search(text), f"{rel} no longer names an old profile ({why}); drop it from ALLOWED"


def test_the_pattern_finds_the_old_names_and_leaves_ordinary_words():
    for bad in ("the review-only profile", "--profile email", "EA_PROFILE=email",
                '"profile": "email"', "the email profile", "profile `email`",
                "`email`, `review-only` or `assistant`"):
        assert OLD.search(bad), bad
    for fine in ("the email address", "an email to the owner", "user-profile.json email field",
                 "profile: name and email", "--profile reviewer", "profile pat@example.edu",
                 "email the reviewer"):
        assert not OLD.search(fine), fine
