"""deploy/apply-host.sh: one root command that runs only the root scripts the
boundary audit names for a failing check, in a fixed order, and audits again."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "apply-host.sh"


def _order():
    return re.search(r'^ORDER="([^"]+)"', SCRIPT.read_text(), re.M).group(1).split()


def _findings(results):
    src = SCRIPT.read_text()
    py = src[src.index("python3 -c '") + len("python3 -c '"):]
    py = py[:py.index("\n' \"$ORDER\"")]
    r = subprocess.run(["python3", "-c", py, " ".join(_order())], input=json.dumps({"results": results}),
                       capture_output=True, text=True)
    return [ln.split("\t") for ln in r.stdout.splitlines()]


def test_every_root_script_it_runs_exists_and_is_explained():
    src = SCRIPT.read_text()
    for name in _order():
        assert (ROOT / "deploy" / f"{name}.sh").is_file(), name
        assert re.search(rf"^\s+{re.escape(name)}\)\s+echo \"", src, re.M), name


def test_a_check_maps_to_its_root_script_and_anything_else_is_manual():
    got = _findings([
        {"status": "PASS", "name": "fine", "fix": "sudo bash deploy/harden-permissions.sh"},
        {"status": "WARN", "name": "auth: pinned", "fix": "sudo bash /opt/ea-system/deploy/pin-auth-dir.sh"},
        {"status": "FAIL", "name": "ssh", "fix": "sudo bash deploy/ssh-deny-resident.sh"},
        {"status": "WARN", "name": "other", "fix": "sudo bash deploy/rename-chat-bot.sh"},
        {"status": "FAIL", "name": "by hand", "fix": "edit the file"},
    ])
    assert [(s, n) for s, n, *_ in got] == [("WARN", "pin-auth-dir"), ("FAIL", "ssh-deny-resident"),
                                            ("WARN", "-"), ("FAIL", "-")]


def test_every_root_script_the_audit_names_is_in_the_order_or_known_manual():
    audit = (ROOT / "scripts" / "boundary-audit.py").read_text()
    # tree-custody.sh is the library the setup scripts source, not a fix
    named = set(re.findall(r"deploy/([a-z-]+)\.sh", audit)) - {"tree-custody"}
    assert named <= set(_order()), named - set(_order())


def test_it_refuses_without_root_and_from_the_residents_tree():
    r = subprocess.run(["bash", str(SCRIPT), "--plan"], capture_output=True, text=True)
    assert r.returncode != 0 and "run as root" in r.stdout
    src = SCRIPT.read_text()
    assert "/home/ea-bot/*) red" in src


def _run_plan(tmp_path, system, audit_results):
    """Run the script's --plan as if root on `system`: fake id, uname and
    sudo on PATH (sudo prints the audit), the real python3 and awk after."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text("#!/bin/sh\necho 0\n")
    (fake / "uname").write_text(f"#!/bin/sh\necho {system}\n")
    (fake / "sudo").write_text("#!/bin/sh\ncat \"$AUDIT_JSON\"\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"results": audit_results}))
    import os
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", SUDO_USER="owner", AUDIT_JSON=str(audit))
    return subprocess.run(["bash", str(SCRIPT), "--plan"], capture_output=True, text=True, env=env)


_MAC_AUDIT = [
    {"status": "WARN", "name": "services: in /opt", "fix": "sudo bash deploy/harden-permissions.sh"},
    {"status": "WARN", "name": "net: policy", "fix": "sudo bash deploy/enable-resident-netpolicy.sh"},
    {"status": "WARN", "name": "ssh: resident", "fix": "sudo bash deploy/ssh-deny-resident.sh"},
    {"status": "WARN", "name": "mail: mediation", "fix": "sudo bash deploy/enable-mail-mediation.sh"},
]


def test_on_macos_the_linux_only_scripts_are_not_steps_and_mediation_still_is(tmp_path):
    """On a macOS install (2026-11-09) the audit named three Linux-only
    scripts; each calls systemctl, nft or the sshd drop-in directory and
    stops part-way. They are printed as not available, with the macOS root
    step, and only mail mediation (which knows launchd) is a step."""
    r = _run_plan(tmp_path, "Darwin", _MAC_AUDIT)
    assert r.returncode == 0, r.stdout + r.stderr
    steps = r.stdout.split("Steps to run", 1)[1].split("Not available", 1)[0]
    assert "enable-mail-mediation" in steps
    for s in ("harden-permissions", "enable-resident-netpolicy", "ssh-deny-resident"):
        assert s not in steps, s
    unavailable = r.stdout.split("Not available on Darwin", 1)[1]
    assert "ssh-deny-resident" in unavailable and "deploy/macos/install-services.sh" in unavailable


def test_on_macos_only_unavailable_findings_is_a_clean_exit(tmp_path):
    r = _run_plan(tmp_path, "Darwin", _MAC_AUDIT[:3])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Steps to run" not in r.stdout and "Not available on Darwin" in r.stdout


def test_on_linux_every_script_is_still_a_step(tmp_path):
    r = _run_plan(tmp_path, "Linux", _MAC_AUDIT)
    assert r.returncode == 0, r.stdout + r.stderr
    steps = r.stdout.split("Steps to run", 1)[1]
    for s in ("harden-permissions", "enable-resident-netpolicy", "ssh-deny-resident", "enable-mail-mediation"):
        assert s in steps, s
    assert "Not available" not in r.stdout
