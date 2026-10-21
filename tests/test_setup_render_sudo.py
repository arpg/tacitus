"""The overlay render in setup runs through the sudoers rule the installer
writes: any command as ea-bot, on Linux as on macOS, because the lanes
run `sudo -u ea-bot env ... claude` and owner-io.sh runs `sudo -n -u
ea-bot sh -c ...`, which a rule naming claude alone refuses. The
documented render command is covered by it, and the render finds
ea-bot's tree with no environment of its own."""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import install_overlay  # noqa: E402

LINUX_HOME = "/home/ea-bot"


def _rules(script: str) -> list[str]:
    """The sudoers lines a setup script writes, ea-bot paths filled in."""
    text = (REPO / script).read_text()
    block = re.search(r"<<SUDOEOF[^\n]*\n(.*?)\nSUDOEOF", text, re.S).group(1)
    lines = [ln for ln in block.splitlines() if ln.strip() and not ln.startswith(("#", "Defaults"))]
    return [ln.replace("$EA_BOT_SCRIPT_DIR", f"{LINUX_HOME}/ea-system") for ln in lines]


def _allowed(rules: list[str], command: str) -> bool:
    """True when a NOPASSWD rule for ea-bot covers `command` as sudo
    matches it: ALL, or the same program with exactly these arguments, or
    the program alone (any arguments)."""
    for rule in rules:
        m = re.match(r"\$REAL_USER ALL=\(ea-bot\) NOPASSWD: (.+)$", rule)
        if not m:
            continue
        spec = m.group(1).strip()
        if spec == "ALL" or spec == command or spec == command.split()[0] and " " not in spec:
            return True
    return False


def _documented_render(name: str) -> str:
    text = (REPO / name).read_text()
    [line] = [ln for ln in text.splitlines()
              if ln.startswith("sudo -u ea-bot ") and "install_overlay.py render" in ln]
    return line[len("sudo -u ea-bot "):]


def test_documented_render_is_allowed_by_both_installers():
    for doc in (".claude/skills/setup/SKILL.md", "docs/SETUP.md"):
        command = _documented_render(doc)
        assert not command.startswith("env "), doc
        for script in ("scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh"):
            assert _allowed(_rules(script), command), (doc, script, command)


def test_both_installers_grant_any_command_as_ea_bot():
    """A claude-only rule makes every cron lane die with "a password is
    required" (owner-io.sh mkdir, sudo -u ea-bot env ... claude). The
    Linux rule is the macOS and Docker rule, and the comment above it
    still names the render command as the documented reason."""
    for script in ("scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh"):
        rules = _rules(script)
        assert [r for r in rules if "NOPASSWD" in r] == ["$REAL_USER ALL=(ea-bot) NOPASSWD: ALL"], script
    text = (REPO / "scripts/install/setup-ea-user.sh").read_text()
    head = text[:text.index("<<SUDOEOF")]
    assert "#   /usr/bin/python3 $EA_BOT_SCRIPT_DIR/preprocessor/install_overlay.py render" in head
    assert "owner-io.sh" in head and "env VAR=... claude" in head
    for caller in ("run-agent.sh", "scripts/ea-bot-shell.sh", "scripts/owner-io.sh"):
        assert "sudo" in (REPO / caller).read_text(), caller


def test_doctor_suggests_the_allowed_command():
    text = (REPO / "scripts" / "ea-doctor.py").read_text()
    assert 'f"sudo -u ea-bot /usr/bin/python3 {code}/preprocessor/install_overlay.py render"' in text


def test_render_finds_the_tree_from_the_passwd_home(monkeypatch, tmp_path):
    monkeypatch.delenv("EA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "someone-else"))
    import pwd
    assert install_overlay._ea_dir() == Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea"
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    assert install_overlay._ea_dir() == tmp_path
