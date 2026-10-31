"""relay/install.sh, its unit and the nginx snippet: the operator's install
of the web chat relay on a public host (docs/webchat-relay.md, Running the
relay). Shape checks only; the script needs root and a host."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / "relay"


def test_the_install_script_parses_and_names_its_parts():
    script = RELAY / "install.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert "set -euo pipefail" in text and '[ "$(id -u)" = 0 ]' in text
    assert "https://github.com/arpg/tacitus.git" in text, "a public clone, no deploy key"
    assert "/etc/tacitus-relay/installs.txt" in text and "--install-id" in text
    assert "relay/tacitus-relay.service" in text and "/healthz" in text


def test_the_unit_runs_the_relay_as_its_own_user_on_loopback():
    unit = (RELAY / "tacitus-relay.service").read_text()
    assert "User=tacitus-relay" in unit and "Restart=always" in unit
    exec_line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
    for flag in ("--bind 127.0.0.1", "--port 8471", "--static /opt/tacitus-relay/webui",
                 "--allow-file /etc/tacitus-relay/installs.txt"):
        assert flag in exec_line, flag
    assert "ProtectSystem=strict" in unit


def test_the_nginx_snippet_streams_and_forwards_the_address():
    conf = (RELAY / "snippets" / "nginx.conf").read_text()
    assert "proxy_pass http://127.0.0.1:8471/;" in conf, "the trailing slash strips the prefix"
    assert "proxy_buffering off;" in conf
    assert "X-Forwarded-For   $proxy_add_x_forwarded_for" in conf
    timeout = next(l for l in conf.splitlines() if "proxy_read_timeout" in l)
    assert int(timeout.split()[1].rstrip("s;")) > 60
