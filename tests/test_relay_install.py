"""relay/install.sh and the nginx snippets: the operator's install of the
web chat relay on a public host (docs/webchat-relay.md, Running the
relay). The script needs root and a host, so these check its shape, run
its argument checks (which come before it changes anything), and check
the unit it writes and the proxy's rules."""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / "relay"
SCRIPT = RELAY / "install.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=30, stdin=subprocess.DEVNULL)


def _unit(static: str = "") -> str:
    """The unit the script writes, with its variables filled in."""
    body = SCRIPT.read_text().split("<<UNIT\n", 1)[1].split("\nUNIT\n", 1)[0]
    for name, value in (("$PY", "/usr/bin/python3"), ("$BASE", "/opt/tacitus-relay"),
                        ("$ETC", "/etc/tacitus-relay"), ("$STATIC", static)):
        body = body.replace(name, value)
    return body + "\n"


def test_the_install_script_parses_and_names_its_parts():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text and '[ "$(id -u)" = 0 ]' in text
    assert 'REPO="https://github.com/arpg/tacitus.git"' in text, "a public clone, no deploy key"
    assert "ETC=/etc/tacitus-relay" in text and "$ETC/installs.txt" in text and "--install-id" in text
    # The unit comes from the script, never from the clone.
    assert not (RELAY / "tacitus-relay.service").exists()
    assert "cat > $UNIT.new <<UNIT" in text and "$CLONE/relay/tacitus-relay" not in text
    # The clone is checked against the commit it was asked for.
    assert '[ "$(git -C $CLONE rev-parse HEAD)" = "$COMMIT" ]' in text
    assert "verify-tag" in text and "gpg.ssh.allowedSignersFile" in text
    # A file is copied beside its place and moved in.
    assert "relay_server.py.new" in text and "mv -f $BASE/relay_server.py.new" in text


def test_the_script_installs_only_a_commit_it_is_named():
    """As a user, every check of the arguments comes before the root check:
    no branch tip, no unsigned tag, no option smuggled into git."""
    cases = [((), "give --commit SHA"),
             (("--commit", "main"), "full commit id"),
             (("--commit", SHA[:39]), "full commit id"),
             (("--commit", "--upload-pack=touch"), "full commit id"),
             (("--commit", SHA, "--tag", "v1.0.0"), "not both"),
             (("--tag", "main"), "release tag"),
             (("--tag", "--upload-pack=touch"), "release tag"),
             (("--commit", SHA, "--install-id", "Not-An-Install"), "not an install id"),
             (("--commit", SHA, "--repo", "http://example.test/tacitus.git"), "https URL"),
             (("--commit", SHA, "--repo", "--upload-pack=touch"), "https URL"),
             (("--bogus",), "unknown argument")]
    if not Path("/etc/tacitus-relay/release-key.pub").exists() \
            and not Path("/var/lib/tacitus-relay/release-key.pub").exists():
        cases.append((("--tag", "v1.0.0"), "release key"))
    for args, says in cases:
        r = _run(*args)
        assert r.returncode != 0 and says in r.stderr, (args, r.stderr)
    if os.geteuid() != 0:
        r = _run("--commit", SHA, "--install-id", "a" * 26, "--with-app")
        assert r.returncode != 0 and "run as root" in r.stderr, r.stderr


def test_the_unit_runs_the_relay_alone_on_loopback():
    unit = _unit()
    lines = unit.splitlines()
    exec_line = next(line for line in lines if line.startswith("ExecStart="))
    assert exec_line.startswith("ExecStart=/usr/bin/python3 -I /opt/tacitus-relay/relay_server.py ")
    for flag in ("--bind 127.0.0.1", "--port 8471", "--allow-file /etc/tacitus-relay/installs.txt"):
        assert flag in exec_line, flag
    assert "--static" not in exec_line, "no app from a relay that shares a host's origin"
    for want in ("User=tacitus-relay", "Restart=always", "IPAddressDeny=any",
                 "IPAddressAllow=localhost", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
                 "CapabilityBoundingSet=", "MemoryMax=768M", "TasksMax=2200", "LimitNOFILE=4096",
                 "NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateUsers=yes",
                 "MemoryDenyWriteExecute=yes", "SystemCallFilter=@system-service", "UMask=0077",
                 "ReadOnlyPaths=/opt/tacitus-relay /etc/tacitus-relay"):
        assert want in lines, want
    served = _unit(" --static /opt/tacitus-relay/webui")
    assert "--allow-file /etc/tacitus-relay/installs.txt --static /opt/tacitus-relay/webui" in served
    text = SCRIPT.read_text()
    assert 'STATIC=" --static $BASE/webui"' in text and "--with-app) WITH_APP=1" in text
    assert "rm -rf $BASE/webui  # this relay serves no app" in text
    if shutil.which("systemd-analyze"):
        path = Path(os.environ.get("TMPDIR", "/tmp")) / f"tacitus-relay-{os.getpid()}.service"
        path.write_text(unit)
        try:
            r = subprocess.run(["systemd-analyze", "verify", str(path)], capture_output=True,
                               text=True, timeout=60)
            assert r.returncode == 0, r.stderr
        finally:
            path.unlink()


def test_the_nginx_snippet_keeps_the_relay_to_itself():
    conf = (RELAY / "snippets" / "nginx.conf").read_text()
    http = (RELAY / "snippets" / "nginx-http.conf").read_text()
    # No regex location of the site takes a relay path; the prefix is stripped.
    assert "location ^~ /relay/ {" in conf and "proxy_pass http://127.0.0.1:8471/;" in conf
    assert "location = /relay/healthz { access_log off; return 404; }" in conf
    for want in ("proxy_ignore_headers X-Accel-Redirect X-Accel-Expires X-Accel-Limit-Rate",
                 "X-Accel-Charset X-Accel-Buffering;", "proxy_hide_header Set-Cookie;",
                 "proxy_hide_header Service-Worker-Allowed;", "proxy_buffering off;",
                 "X-Forwarded-For   $proxy_add_x_forwarded_for",
                 "limit_req zone=tacitus_relay burst=100 nodelay;",
                 "limit_conn tacitus_relay_conn 32;", "client_max_body_size 8k;",
                 "access_log off;", 'if ($http_authorization = "") { return 401; }',
                 "if ($tacitus_relay_nokey) { return 401; }"):
        assert want in conf, want
    # 16 MiB only on the bot's poll and a phone's frame.
    assert conf.count("client_max_body_size 16m;") == 2
    for route in ("bot", "c", "sse"):
        assert f'location ~ "^/relay/r/[a-z2-7]{{26}}/{route}$" {{' in conf, route
    timeout = next(line for line in conf.splitlines() if "proxy_read_timeout" in line)
    assert int(timeout.split()[1].rstrip("s;")) > 60
    # What the server block names, the http block defines.
    assert "limit_req_zone $binary_remote_addr zone=tacitus_relay:" in http
    assert "limit_conn_zone $binary_remote_addr zone=tacitus_relay_conn:" in http
    assert 'map "$request_method:$http_x_relay_key" $tacitus_relay_nokey {' in http
    assert re.search(r'"POST:" 1;', http)


def _heredoc(marker: str) -> str:
    """The body of the heredoc that writes `marker`, with the script's
    variables filled in."""
    text = SCRIPT.read_text()
    body = text.split(marker, 1)[1].split("\nUNIT\n", 1)[0]
    return body.replace("$PY", "/usr/bin/python3").replace("$CLONE", "/opt/tacitus-relay/src") + "\n"


def test_the_script_installs_the_updater_by_default():
    """The relay host's updater (docs/updates.md, The relay host): the
    script writes the two units itself, with the text of the files in
    relay/, and enables the timer; --no-update-timer leaves them out and
    removes an installed one."""
    text = SCRIPT.read_text()
    assert "UPDATE_TIMER=1" in text and "--no-update-timer) UPDATE_TIMER=0" in text
    assert "UPDATER=/etc/systemd/system/tacitus-relay-update" in text
    service = _heredoc("cat > $UPDATER.service.new <<UNIT\n")
    timer = _heredoc("cat > $UPDATER.timer.new <<'UNIT'\n")
    assert service == (RELAY / "tacitus-relay-update.service").read_text()
    assert timer == (RELAY / "tacitus-relay-update.timer").read_text()
    assert "ExecStart=/usr/bin/python3 -I /opt/tacitus-relay/src/scripts/update.py relay" in service
    assert "OnCalendar=hourly" in timer and "RandomizedDelaySec=55min" in timer
    # Written beside their place and moved in, never copied from the clone.
    assert "$CLONE/relay/tacitus-relay-update" not in text
    assert "mv -f $UPDATER.service.new $UPDATER.service" in text
    assert "mv -f $UPDATER.timer.new $UPDATER.timer" in text
    on, off = text.split("if [ $UPDATE_TIMER = 1 ]; then", 1)[1].split("\nelse\n", 1)
    assert "systemctl enable --now tacitus-relay-update.timer" in on
    off = off.split("\nfi\n", 1)[0]
    assert "systemctl disable --now tacitus-relay-update.timer" in off
    assert "rm -f $UPDATER.service $UPDATER.timer" in off
    if shutil.which("systemd-analyze"):
        d = Path(os.environ.get("TMPDIR", "/tmp")) / f"relay-update-{os.getpid()}"
        d.mkdir()
        try:
            (d / "tacitus-relay-update.service").write_text(service)
            (d / "tacitus-relay-update.timer").write_text(timer)
            r = subprocess.run(["systemd-analyze", "verify", str(d / "tacitus-relay-update.timer"),
                                str(d / "tacitus-relay-update.service")],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr
        finally:
            shutil.rmtree(d)


def test_the_new_flags_pass_the_argument_checks():
    if os.geteuid() == 0:
        return
    for extra in (("--no-update-timer",), ("--without-app",), ("--with-app", "--no-update-timer")):
        r = _run("--commit", SHA, *extra)
        assert r.returncode != 0 and "run as root" in r.stderr, (extra, r.stderr)


def test_an_update_keeps_what_the_relay_serves_and_reports_its_health():
    """The updater runs a new release's install.sh --tag with no other
    flag: the relay keeps serving the app when its unit did, takes the key
    the updater pinned, and the script's exit says whether the relay
    answered."""
    text = SCRIPT.read_text()
    assert 'WITH_APP=""' in text and "--without-app) WITH_APP=0" in text
    keep = text.index('grep -q -- " --static " "$UNIT"')
    assert text.index('[ "$(id -u)" = 0 ]') < keep < text.index("cat > $UNIT.new <<UNIT")
    assert "PINNED_KEY=/var/lib/tacitus-relay/release-key.pub" in text
    assert '[ -f "$KEY" ] || KEY=$PINNED_KEY' in text
    # Every key line of the pin, not only the first.
    assert "head -n 1 \"$KEY\"" not in text
    assert "sed 's/^/tacitus-release namespaces=\"git\" /'" in text
    assert text.rstrip().endswith("[ $HEALTHY = 1 ] || exit 1")
    assert "Releases are not signed yet" not in text
