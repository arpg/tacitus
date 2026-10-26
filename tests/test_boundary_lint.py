"""The owner trust rules (docs/SECURITY.md) hold in the code, and the check
that says so keeps its teeth.

scripts/boundary_lint.py reports every place that bypasses the rules. The
repository may carry only the hits tests/boundary_baseline.json accepts,
and every accepted line must still match a hit, so the list only shrinks.
The rest pins what the check detects and what it leaves alone, on small
sources written here, so a change that blinds a rule fails too.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import boundary_lint as bl  # noqa: E402

BASELINE = REPO / "tests" / "boundary_baseline.json"


def _py(sources: dict) -> list:
    hits, errors = bl.lint_python(sources)
    assert not errors, errors
    return sorted((h.file, h.rule, h.code) for h in hits)


def _rules(hits) -> list:
    return sorted({h[1] for h in hits})


# ── the repository against the baseline ─────────────────────────────────

def test_no_hit_outside_the_baseline():
    hits, errors = bl.lint()
    assert not errors, errors
    new, _ = bl.compare(hits, bl.load_baseline(BASELINE))
    assert not new, ("new owner trust bypasses (use the named helper, or add an accepted "
                     "exception with its reason to tests/boundary_baseline.json):\n"
                     + "\n".join(h.render() for h in new))


def test_every_baseline_line_still_matches_a_hit():
    hits, _ = bl.lint()
    _, stale = bl.compare(hits, bl.load_baseline(BASELINE))
    assert not stale, ("baseline lines no hit matches any more; remove them from "
                       "tests/boundary_baseline.json:\n"
                       + "\n".join(f"{f} {r}: {line}" for f, r, line in stale))


def test_baseline_entries_are_well_formed():
    doc = json.loads(BASELINE.read_text())
    seen = set()
    for e in doc["exceptions"]:
        assert set(e) == {"file", "rule", "reason", "lines"}, e
        assert (REPO / e["file"]).is_file(), e["file"]
        assert e["rule"] in bl.RULES, e["rule"]
        assert (e["file"], e["rule"]) not in seen, f"one entry per file and rule: {e['file']}"
        seen.add((e["file"], e["rule"]))
        reason = e["reason"]
        assert reason.strip() and "\n" not in reason and len(reason) <= 200, reason
        assert "\u2014" not in reason and " -- " not in reason, reason
        assert e["lines"] and all(line == bl._norm(line) for line in e["lines"]), e


# ── Python rules ────────────────────────────────────────────────────────

HEAD = "import os, json, sqlite3, tempfile\nfrom pathlib import Path\n" \
       "EA_DIR = Path(os.environ.get('EA_DIR', str(Path.home() / '.ea')))\n"


def test_a_write_by_path_into_resident_space_is_a_hit():
    src = HEAD + ("STATE = EA_DIR / 'state' / 'x.json'\n"
                  "def save(d):\n    STATE.write_text(json.dumps(d))\n"
                  "def log(line):\n    with open(EA_DIR / 'logs' / 'x.log', 'a') as f:\n"
                  "        f.write(line)\n")
    hits = _py({"preprocessor/owner_mod.py": src})
    assert _rules(hits) == ["py-write"] and len(hits) == 2, hits


def test_the_helpers_and_owner_private_paths_are_not_hits():
    src = HEAD + ("import resident_io as _rio, atomic_io as _atomic\n"
                  "import owner_trust\n"
                  "def save(d):\n    _rio.replace_text(EA_DIR / 'state' / 'x.json', json.dumps(d))\n"
                  "    _atomic.write_json(EA_DIR / 'state' / 'y.json', d)\n"
                  "def private(d):\n    (owner_trust.private_dir() / 'z.json').write_text('x')\n"
                  "def scratch():\n    d = tempfile.mkdtemp()\n"
                  "    sqlite3.connect(os.path.join(d, 'x.db'))\n")
    helpers = {"preprocessor/resident_io.py": "def replace_text(p, t):\n    pass\n",
               "preprocessor/atomic_io.py": "def write_json(p, o):\n    pass\n",
               "preprocessor/owner_trust.py":
                   "import os\nfrom pathlib import Path\n"
                   "def private_dir():\n    return Path(os.environ.get('EA_PRIVATE_DIR', "
                   "'/home/x/.ea-private'))\n"}
    assert _py({"preprocessor/owner_mod.py": src, **helpers}) == []


def test_the_resident_side_of_an_owner_check_is_not_a_hit():
    src = HEAD + ("import resident_io as _rio\n"
                  "def a(p):\n    if not _rio.is_owner():\n"
                  "        (EA_DIR / 'x').write_text('x')\n        return\n"
                  "    _rio.replace_text(EA_DIR / 'x', 'x')\n"
                  "def b():\n    if _rio.is_owner():\n        return _rio.replace_text(EA_DIR / 'y', 'y')\n"
                  "    (EA_DIR / 'y').write_text('y')\n"
                  "def c():\n    owner = _rio.is_owner()\n    if owner:\n        pass\n"
                  "    else:\n        (EA_DIR / 'z').write_text('z')\n")
    assert _py({"preprocessor/owner_mod.py": src}) == []
    owner_side = HEAD + ("import resident_io as _rio\n"
                         "def a():\n    if _rio.is_owner():\n        (EA_DIR / 'x').write_text('x')\n")
    assert _rules(_py({"preprocessor/owner_mod.py": owner_side})) == ["py-write"]


def test_a_read_that_is_written_back_is_a_hit():
    src = HEAD + ("import atomic_io as _atomic\nSTATE = EA_DIR / 'state' / 'x.json'\n"
                  "def load():\n    return json.loads(STATE.read_text())\n"
                  "def save(d):\n    _atomic.write_json(STATE, d)\n")
    assert _rules(_py({"preprocessor/owner_mod.py": src})) == ["py-rmw-read"]


def test_sqlite_trust_anchors_env_files_and_git_are_hits():
    src = HEAD + ("from dotenv import load_dotenv\nimport subprocess\n"
                  "AUTH = EA_DIR / 'auth'\n"
                  "def a():\n    return sqlite3.connect(str(EA_DIR / 'state' / 'l.db'))\n"
                  "def b():\n    return json.loads((AUTH / 'token.json').read_text())\n"
                  "def c():\n    return (EA_DIR / '.env').read_text()\n"
                  "def d():\n    load_dotenv(EA_DIR / '.env')\n"
                  "def e():\n    subprocess.run(['git', '-C', str(EA_DIR / 'repo'), 'status'])\n"
                  "def f():\n    subprocess.run(['bash', '-c', 'source ~/.ea/.env && run'])\n")
    rules = [h[1] for h in _py({"preprocessor/owner_mod.py": src})]
    assert sorted(rules) == ["py-env-exec", "py-env-exec", "py-env-file", "py-git",
                             "py-sqlite", "py-trust-anchor"], rules


def test_a_check_by_path_then_a_use_by_path_is_a_hit():
    src = ("import owner_trust\n"
           "def read(path):\n    owner_trust.check_owner_file(path)\n    return path.read_text()\n")
    assert _rules(_py({"preprocessor/owner_mod.py": src})) == ["py-check-then-use"]


def test_a_resident_path_handed_to_a_writer_is_a_hit_at_the_call():
    writer = "def save(path, text):\n    path.write_text(text)\n" \
             "def relay(p):\n    save(p, 'x')\n"
    caller = HEAD + ("from writer import relay\n"
                     "def go():\n    relay(EA_DIR / 'state' / 'x.json')\n")
    hits = _py({"preprocessor/writer.py": writer, "preprocessor/caller.py": caller})
    assert hits == [("preprocessor/caller.py", "py-write",
                     "relay(EA_DIR / 'state' / 'x.json')")], hits


def test_constants_follow_imports_across_modules():
    paths = HEAD + "STATE_DIR = EA_DIR / 'state'\ndef queue():\n    return STATE_DIR / 'q.json'\n"
    user = "import paths\nfrom paths import STATE_DIR\n" \
           "def a():\n    (STATE_DIR / 'x').touch()\n" \
           "def b():\n    paths.queue().write_text('x')\n"
    hits = _py({"preprocessor/paths.py": paths, "preprocessor/user.py": user})
    assert [h[:2] for h in hits] == [("preprocessor/user.py", "py-write")] * 2, hits


def test_a_resident_only_module_stays_out_of_owner_code():
    tool = HEAD + "def open_pr():\n    (EA_DIR / 'review').mkdir()\n"
    owner = "import doc_review\n"
    runner = "import subprocess\ndef run():\n" \
             "    subprocess.run(['python3', '/x/preprocessor/doc_review.py', 'list'])\n" \
             "def run_as_resident():\n" \
             "    subprocess.run(['sudo', '-n', '-u', 'ea-bot', 'python3', " \
             "'/x/preprocessor/doc_review.py'])\n"
    hits = _py({"preprocessor/doc_review.py": tool, "preprocessor/owner.py": owner,
                "preprocessor/runner.py": runner})
    assert [h[:2] for h in hits] == [("preprocessor/owner.py", "py-resident-module"),
                                     ("preprocessor/runner.py", "py-resident-module")], hits


# ── shell and unit rules ────────────────────────────────────────────────

def _sh(text: str, rel: str = "scripts/owner.sh") -> list:
    return sorted(h.rule for h in bl.lint_shell(rel, text))


def test_shell_reads_writes_and_sourcing_in_resident_space_are_hits():
    assert _sh('source "$EA_DIR/.env"\n') == ["sh-source"]
    assert _sh('. ~/.ea/.env.secrets\n') == ["sh-source"]
    assert _sh('eval "$(cat "$EA_DIR/.env")"\n') == ["sh-source"]
    assert _sh('echo x > "$EA_DIR/state/y"\n') == ["sh-io"]
    assert _sh('STATE_DIR="$EA_DIR/state"\nmkdir -p "$STATE_DIR"\n') == ["sh-io"]
    assert _sh('cat /home/ea-bot/.ea/state/x.json\n') == ["sh-io"]
    assert _sh('git -C "$EA_BOT_SCRIPT_DIR" fetch origin\n') == ["sh-git"]
    assert _sh('sudo mkdir -p /home/ea-bot/.ea/x\n') == ["sh-io"]


def test_shell_access_as_the_resident_or_through_owner_io_is_not_a_hit():
    assert _sh('printf x | "$SCRIPT_DIR/scripts/owner-io.sh" write "$EA_DIR/state/y"\n') == []
    assert _sh('sudo -n -u ea-bot mkdir -p "$EA_DIR/x"\n') == []
    assert _sh('LOG_DIR="$HOME/.ea-private/logs"\nmkdir -p "$LOG_DIR"\necho x >> "$LOG_DIR/a"\n') == []
    assert _sh('log "moved $A -> $EA_DIR/state/b"\n') == []            # a quoted arrow
    assert _sh("python3 - <<'PY'\nopen('/home/ea-bot/.ea/x', 'w')\nPY\n") == []
    assert _sh('# echo x > "$EA_DIR/state/y"\n') == []


def test_units_and_crontab_lines_are_checked_wherever_they_are_written():
    assert _sh("cat > u.service <<EOF\nEnvironmentFile=%h/.ea/.env\nEOF\n") == ["unit-envfile"]
    assert _sh("cat > u.service <<EOF\nEnvironmentFile=-%h/.ea-private/secrets.env\n"
               "EnvironmentFile=\nEOF\n") == []
    assert _sh('PRIV="$HOME/.ea-private"\nENVF="$PRIV/bot.env"\n'
               'cat > u <<EOF\nEnvironmentFile=$ENVF\nEOF\n') == []
    cron = 'ENTRIES="\n0 * * * * $SCRIPT_DIR/run-agent.sh x >> $EA_DIR/logs/x.log 2>&1\n"\n'
    assert _sh(cron) == ["cron-log"]
    assert _sh(cron.replace("$EA_DIR/logs", "$HOME/.ea-private/logs")) == []


def test_terminal_tools_are_checked_only_for_what_they_hand_on(tmp_path):
    (tmp_path / "scripts" / "install").mkdir(parents=True)
    (tmp_path / "scripts/install/setup.sh").write_text(
        '#!/usr/bin/env bash\nmkdir -p "$EA_DIR/state"\n'
        'E="\n0 * * * * run.sh >> $EA_DIR/logs/x.log 2>&1\n"\n')
    (tmp_path / "run-agent.sh").write_text('#!/usr/bin/env bash\nmkdir -p "$EA_DIR/state"\n')
    hits, _ = bl.lint(tmp_path)
    assert sorted((h.file, h.rule) for h in hits) == [("run-agent.sh", "sh-io"),
                                                      ("scripts/install/setup.sh", "cron-log")], hits


# ── the CLI and the hook ────────────────────────────────────────────────

def test_the_staged_check_blocks_a_new_hit_and_passes_a_clean_file(monkeypatch, capsys):
    bad = HEAD + "(EA_DIR / 'state' / 'new.json').write_text('x')\n"
    monkeypatch.setattr(bl, "staged", lambda: {"preprocessor/new_owner_mod.py": bad})
    assert bl.main(["--staged"]) == 1
    assert "preprocessor/new_owner_mod.py:4: py-write" in capsys.readouterr().out
    monkeypatch.setattr(bl, "staged", lambda: {"preprocessor/new_owner_mod.py": HEAD})
    assert bl.main(["--staged"]) == 0


def test_the_pre_commit_hook_runs_the_staged_check():
    hook = (REPO / ".githooks" / "pre-commit").read_text()
    assert 'scripts/boundary_lint.py" --staged || exit 1' in hook
