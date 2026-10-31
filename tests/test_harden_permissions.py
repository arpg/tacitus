"""The deploy hook and the harden script keep the services off ea-bot's tree.

Gaps closed: the services load their environment from a crh-owned file and
run their code and interpreter from crh-owned paths (/opt, private home),
never from /home/ea-bot, whose parent ea-bot owns. The deploy hook mirrors
the committed tree into the crh-owned live path on every commit.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = (REPO / ".githooks" / "post-commit").read_text()
HARDEN = (REPO / "deploy" / "harden-permissions.sh").read_text()


def test_hook_mirrors_the_committed_tree_into_the_crh_owned_live_path():
    assert 'LIVE_DIR="/opt/ea-system"' in HOOK
    # the live tree comes from `git archive` of the committer's repo, never
    # from ea-bot's copy
    assert 'rsync -a --delete --exclude ".git" "$EXPORT_DIR/" "$LIVE_DIR/"' in HOOK
    assert '"$DEPLOY_DIR/" "$LIVE_DIR/"' not in HOOK
    assert 'archive --format=tar HEAD' in HARDEN and '"$EXPORT/" "$LIVE/"' in HARDEN
    assert '"$EABOT_TREE/" "$LIVE/"' not in HARDEN
    # a failed live sync must stop the deploy, like a failed fetch
    seg = HOOK[HOOK.index("LIVE SYNC FAILED"):]
    assert "exit 1" in seg[:120]
    # the live tree has no .git, so the deploy leaves the commit beside the code
    assert 'rev-parse --short HEAD > "$LIVE_DIR/.deployed-commit"' in HOOK
    assert 'rev-parse --short HEAD > "$LIVE/.deployed-commit"' in HARDEN
    assert 'git -C "$EABOT_TREE"' not in HARDEN


def test_harden_moves_both_services_off_ea_bot_tree():
    assert HARDEN.startswith("#!/usr/bin/env bash")
    assert 'if [ "$(id -u)" -ne 0 ]' in HARDEN          # must run as root
    # code + interpreter come from crh-owned paths, never /home/ea-bot
    assert "LIVE=/opt/ea-system" in HARDEN
    assert "VENV_PY=" in HARDEN and "systemctl show -p ExecStart --value ea-chat-bot" in HARDEN
    for unit in ("ea-chat-bot.service.d", "ea-mail-mediator.service.d"):
        assert unit in HARDEN
    # ExecStart is reset then repointed at the live tree for both units; the
    # updater's own unit (docs/updates.md) runs from the live tree too
    assert HARDEN.count("ExecStart=\n") == 2
    assert HARDEN.count("ExecStart=$VENV_PY $LIVE/") == 3
    assert "ExecStart=$VENV_PY $LIVE/scripts/update.py run" in HARDEN
    # the bot's environment file is crh-owned, and the ea-bot .env is dropped
    assert "BOT_ENV=\"$OWNER_HOME/.ea-private/bot.env\"" in HARDEN
    assert "EnvironmentFile=\n" in HARDEN and "EnvironmentFile=$BOT_ENV" in HARDEN
    assert 'chown "$OWNER":"$OWNER" "$BOT_ENV"; chmod 600 "$BOT_ENV"' in HARDEN
    # no service is left pointing into ea-bot's tree for code or interpreter
    assert "$EABOT_TREE/ea-chat-bot.py" in HARDEN  # references, does not RUN from ea-bot tree
    assert "OWNER=\"${SUDO_USER:-}\"" in HARDEN  # owner-agnostic
    # it verifies the result and is reversible
    assert "systemctl is-active" in HARDEN and "revert:" in HARDEN


SANDBOX_SH = REPO / "deploy" / "mediator-sandbox.sh"
ENABLE = (REPO / "deploy" / "enable-mail-mediation.sh").read_text()


def _sandbox_lines() -> list[str]:
    import subprocess
    out = subprocess.run(
        ["bash", "-c", f'. "{SANDBOX_SH}"; mediator_sandbox /home/owner /home/ea-bot'],
        capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def test_mediator_unit_runs_in_a_sandbox_both_installers_share():
    lines = _sandbox_lines()
    keys = {ln.split("=", 1)[0] for ln in lines}
    assert "ProtectSystem=strict" in lines and "ProtectHome=read-only" in lines
    writable = [p.lstrip("-") for ln in lines if ln.startswith("ReadWritePaths=")
                for p in ln.split("=", 1)[1].split()]
    for need in ("/home/ea-bot", "/tmp", "/home/owner/.ea-private",
                 "/home/owner/.ea-mediator-staging"):
        assert need in writable, need
    # nothing inside ea-bot's home is named: systemd follows a symlink there
    assert not [p for p in writable if p.startswith("/home/ea-bot/")]
    # no other path under the owner's home is writable
    assert not [p for p in writable if p.startswith("/home/owner")
                and p not in ("/home/owner/.ea-private", "/home/owner/.ea-mediator-staging")]
    assert "InaccessiblePaths=-/home/owner/.ssh -/home/owner/.gnupg" in lines
    # sudo -u ea-bot carries the resident-side file copies, and no_new_privs
    # stops sudo; /tmp is an allowed root the resident must see
    for off in ("NoNewPrivileges", "PrivateTmp", "SystemCallFilter", "PrivateDevices",
                "RestrictSUIDSGID", "RestrictNamespaces", "ProtectKernelModules",
                "LockPersonality", "MemoryDenyWriteExecute", "RestrictAddressFamilies",
                "RemoveIPC", "CapabilityBoundingSet", "ProtectProc"):
        assert off not in keys, off
    # both installers source the one file and write its lines into the unit
    assert '. "$REPO_DIR/deploy/mediator-sandbox.sh"' in HARDEN
    assert '$(mediator_sandbox "$OWNER_HOME" "$EABOT_HOME")' in HARDEN
    assert 'mediator_sandbox_dirs "$OWNER" "$OWNER_HOME"' in HARDEN
    assert '. "$SRC/deploy/mediator-sandbox.sh"' in ENABLE
    assert '$(mediator_sandbox "$REAL_HOME" "$EA_BOT_HOME")' in ENABLE
    assert 'mediator_sandbox_dirs "$REAL_USER" "$REAL_HOME"' in ENABLE
    # the harden run checks the sandbox took effect
    assert "systemctl show -p ProtectSystem --value" in HARDEN
