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
    for extra in (("--no-update-timer",), ("--without-app",), ("--with-app", "--no-update-timer"),
                  ("--no-nginx",), ("--nginx",)):
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
    assert text.rstrip().endswith("[ $HEALTHY = 1 ] && [ $NGINX_OK = 1 ] || exit 1")
    assert "Releases are not signed yet" not in text


# ── the signed tag check (verify_tag, run alone) ─────────────────────

def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
                       cwd=str(cwd), capture_output=True, text=True, env=env)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def _verify_tag(clone, tag, key, expect=""):
    """relay/install.sh's verify_tag function, cut out and run on its own."""
    text = SCRIPT.read_text()
    fn = text.split("# >>> verify_tag", 1)[1].split("# <<< verify_tag", 1)[0]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["bash", "-c", fn + '\nverify_tag "$@"', "verify_tag", str(clone), tag,
                           str(key), expect], capture_output=True, text=True, env=env, timeout=30)


def test_the_script_takes_only_a_tag_under_its_own_name_on_main(tmp_path):
    """The review's finding: --tag took an older signed tag object moved to
    a new name. The tag must carry its own name inside, name the commit the
    updater verified when it says one, sit on main's history, and be signed
    by an ssh-ed25519 key of the pin."""
    if not shutil.which("ssh-keygen"):
        return
    key = tmp_path / "release"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "release", "-f", str(key)],
                   check=True)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    sign = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}")
    (origin / "f").write_text("1")
    _git(origin, "add", "f")
    _git(origin, "commit", "-qm", "one")
    old = _git(origin, "rev-parse", "HEAD")
    _git(origin, *sign, "tag", "-s", "-m", "v1.0.0", "v1.0.0")
    (origin / "f").write_text("2")
    _git(origin, "commit", "-qam", "two")
    new = _git(origin, "rev-parse", "HEAD")
    _git(origin, *sign, "tag", "-s", "-m", "v1.0.1", "v1.0.1")
    # A signed tag on a commit that never reached main.
    _git(origin, "checkout", "-q", "-b", "side")
    (origin / "f").write_text("side")
    _git(origin, "commit", "-qam", "side")
    _git(origin, *sign, "tag", "-s", "-m", "v1.0.2", "v1.0.2")
    _git(origin, "checkout", "-q", "main")
    # The older signed tag object under a newer name.
    _git(origin, "update-ref", "refs/tags/v1.0.3", _git(origin, "rev-parse", "refs/tags/v1.0.0"))
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", str(clone))
    _git(clone, "fetch", "-q", "--no-tags", str(origin), "+refs/tags/*:refs/tags/*",
         "+refs/heads/main:refs/remotes/origin/main")
    pub = Path(str(key) + ".pub")

    ok = _verify_tag(clone, "v1.0.1", pub)
    assert ok.returncode == 0 and ok.stdout.strip() == new, ok.stderr
    assert _verify_tag(clone, "v1.0.1", pub, new).returncode == 0
    for tag, expect, says in (("v1.0.3", "", "carries another name"),
                              ("v1.0.1", old, f"not {old}"),
                              ("v1.0.2", "", "not in main's history")):
        r = _verify_tag(clone, tag, pub, expect)
        assert r.returncode != 0 and says in r.stderr, (tag, r.stderr)
    # Only ssh-ed25519 lines of the pin count.
    rsa = tmp_path / "rsa.pub"
    rsa.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ test\n")
    r = _verify_tag(clone, "v1.0.1", rsa)
    assert r.returncode != 0 and "no ssh-ed25519 key" in r.stderr
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    r = _verify_tag(clone, "v1.0.1", Path(str(other) + ".pub"))
    assert r.returncode != 0 and "not signed by the release key" in r.stderr
    # The flag is checked with the other arguments, before anything changes.
    for args, says in ((("--commit", SHA, "--expect-commit", SHA), "with --tag"),
                       (("--tag", "v1.0.1", "--expect-commit", "main"), "full commit id")):
        r = _run(*args)
        assert r.returncode != 0 and says in r.stderr, (args, r.stderr)
    text = SCRIPT.read_text()
    assert 'COMMIT="$(verify_tag $CLONE "$TAG" "$KEY" "$EXPECT")"' in text
    assert "+refs/heads/main:refs/remotes/origin/main" in text


def test_the_nginx_snippet_warns_against_a_verbose_error_log():
    """limit_req logs its refusals with the request line at info: the
    snippet keeps them there and says to leave error_log above it."""
    text = (RELAY / "snippets" / "nginx.conf").read_text()
    assert "limit_req_log_level info;" in text and "error_log above info" in text


# ── the nginx snippets: files of their own, each change past nginx -t ─

# nginx -t fails while a .conf file under the config holds BROKEN: it reads
# what nginx would, so a staged .new or a kept .old never counts.
FAKE_NGINX = r"""#!/bin/bash
echo "nginx $*" >> "$CALLS"
if f="$(grep -Rls --include='*.conf' BROKEN "$FAKE_CONF")"; then
    echo "nginx: [emerg] unknown directive \"BROKEN\" in ${f%%$'\n'*}:1" >&2
    echo "nginx: configuration file $FAKE_CONF/nginx.conf test failed" >&2
    exit 1
fi
echo "nginx: configuration file $FAKE_CONF/nginx.conf test is successful" >&2
"""
FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$CALLS"
if [ "$1" = is-active ]; then [ "$NGINX_ACTIVE" = 1 ]; fi
"""
# The tools the function and the script's last part run; nothing else is on PATH.
TOOLS = ("grep", "sed", "mkdir", "chmod", "install", "cmp", "mv", "cp", "rm", "cat")


def _cut(name: str) -> str:
    """A marked function of relay/install.sh, without its marker lines."""
    body = SCRIPT.read_text().split(f"# >>> {name}", 1)[1].split("\n", 1)[1]
    return body.split(f"# <<< {name}", 1)[0]


class _Host:
    """A relay host under tmp_path: the clone's snippets, nginx's config
    directory with the vendored one inside, and a PATH of the tools alone,
    with a fake nginx (absent when nginx=False) and a fake systemctl that
    log their calls."""

    def __init__(self, tmp_path: Path, nginx: bool = True):
        self.tmp = tmp_path
        self.clone = tmp_path / "src"
        self.src = self.clone / "relay" / "snippets"
        self.src.mkdir(parents=True)
        for f in (RELAY / "snippets").iterdir():
            shutil.copy(f, self.src / f.name)
        self.conf = tmp_path / "etc-nginx"
        self.conf.mkdir()
        (self.conf / "nginx.conf").write_text("http {\n    include mime.types;\n}\n")
        self.dir = self.conf / "tacitus-relay"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self._shim("systemctl", FAKE_SYSTEMCTL)
        if nginx:
            self._shim("nginx", FAKE_NGINX)
        self.calls_file = tmp_path / "calls"
        self.calls_file.write_text("")
        self.active = True

    def _shim(self, name: str, text: str) -> None:
        (self.bin / name).write_text(text)
        (self.bin / name).chmod(0o755)

    def env(self) -> dict:
        return {"PATH": str(self.bin), "CALLS": str(self.calls_file), "FAKE_CONF": str(self.conf),
                "NGINX_ACTIVE": "1" if self.active else "0", "LC_ALL": "C"}

    def calls(self) -> list:
        lines = self.calls_file.read_text().splitlines()
        self.calls_file.write_text("")
        return lines

    def run(self, mode: str = "on") -> subprocess.CompletedProcess:
        """nginx_snippets as the script calls it: in a || list, under
        set -euo pipefail."""
        code = "set -euo pipefail\n" + _cut("nginx_snippets") + '\nnginx_snippets "$@" || exit $?\n'
        return subprocess.run([shutil.which("bash"), "-c", code, "nginx_snippets", str(self.src),
                               str(self.dir), str(self.conf), mode],
                              capture_output=True, text=True, env=self.env(), timeout=30)

    def leftovers(self) -> list:
        return sorted(p.name for p in self.dir.iterdir() if p.suffix in (".new", ".old"))


def test_the_script_vendors_the_snippets_beside_nginx():
    text = SCRIPT.read_text()
    assert "NGINX_DIR=/etc/nginx/tacitus-relay" in text and "NGINX_CONF=/etc/nginx" in text
    assert "BASE=/opt/tacitus-relay" in text and "CLONE=$BASE/src" in text
    # The clone's snippets go in through the gate, after the relay's own unit.
    call = "nginx_snippets $CLONE/relay/snippets $NGINX_DIR $NGINX_CONF $NGINX || NGINX_OK=0"
    assert call in text
    assert text.index("systemctl restart tacitus-relay.service") < text.index(call)
    assert text.index("systemctl enable --now tacitus-relay-update.timer") < text.index(call)
    # --no-nginx holds for the updater's later runs, which pass no such flag.
    assert "--no-nginx) NGINX=off" in text and "--nginx) NGINX=on" in text
    assert "if [ -e $ETC/no-nginx ]; then NGINX=off; else NGINX=on; fi" in text
    assert "Now put relay/snippets" not in text, "no include from the clone"
    for snippet, name in (("nginx-http.conf", "http.conf"), ("nginx.conf", "server.conf")):
        head = (RELAY / "snippets" / snippet).read_text().split("\n\n", 1)[0]
        assert f"/etc/nginx/tacitus-relay/{name}" in head and "never" in head, snippet


def test_unchanged_snippets_are_not_rewritten(tmp_path):
    host = _Host(tmp_path)
    r = host.run()
    assert r.returncode == 0, r.stderr
    for name, snippet in (("http.conf", "nginx-http.conf"), ("server.conf", "nginx.conf")):
        assert (host.dir / name).read_text() == (host.src / snippet).read_text()
    assert host.calls() == ["nginx -t", "systemctl is-active --quiet nginx", "systemctl reload nginx"]
    before = {}
    for name in ("http.conf", "server.conf"):
        os.utime(host.dir / name, (1_000_000_000, 1_000_000_000))
        st = (host.dir / name).stat()
        before[name] = (st.st_ino, st.st_mtime_ns)
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert "match this release; nginx left alone." in r.stdout
    assert host.calls() == [], "no nginx -t and no reload for snippets that match"
    for name, (ino, mtime) in before.items():
        st = (host.dir / name).stat()
        assert (st.st_ino, st.st_mtime_ns) == (ino, mtime), name
    assert host.leftovers() == []


def test_a_changed_snippet_passes_nginx_t_and_nginx_reloads(tmp_path):
    host = _Host(tmp_path)
    assert host.run().returncode == 0
    host.calls()
    os.utime(host.dir / "http.conf", (1_000_000_000, 1_000_000_000))
    with open(host.src / "nginx.conf", "a") as f:
        f.write("# a later release's line\n")
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert (host.dir / "server.conf").read_text() == (host.src / "nginx.conf").read_text()
    assert (host.dir / "http.conf").stat().st_mtime_ns == 1_000_000_000 * 10**9, "only the changed file"
    assert host.calls() == ["nginx -t", "systemctl is-active --quiet nginx", "systemctl reload nginx"]
    assert r.stdout.splitlines()[0] == \
        f"nginx: server.conf in {host.dir} changed, passed nginx -t, and nginx reloaded."
    assert host.leftovers() == []
    # A stopped nginx gets the gate and no reload.
    host.active = False
    with open(host.src / "nginx-http.conf", "a") as f:
        f.write("# one more\n")
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert "nginx is not running, so no reload." in r.stdout
    assert host.calls() == ["nginx -t", "systemctl is-active --quiet nginx"]


def test_a_snippet_that_fails_nginx_t_is_held_back(tmp_path):
    host = _Host(tmp_path)
    assert host.run().returncode == 0
    host.calls()
    old = (host.dir / "server.conf").read_text()
    with open(host.src / "nginx.conf", "a") as f:
        f.write("BROKEN;\n")
    r = host.run()
    assert r.returncode == 1
    assert (host.dir / "server.conf").read_text() == old, "the previous file is back"
    assert host.calls() == ["nginx -t", "nginx -t"], "the old config is checked again; no reload"
    assert f"nginx: nginx -t failed with this release's server.conf, so {host.dir} holds" in r.stderr
    assert "and nginx -t passes with that. nginx keeps the config it runs." in r.stderr
    assert f'  nginx: [emerg] unknown directive "BROKEN" in {host.dir}/server.conf:1' in r.stderr
    assert host.leftovers() == []
    # Run again: still held back, still the old file.
    r = host.run()
    assert r.returncode == 1 and (host.dir / "server.conf").read_text() == old


def test_a_gate_that_fails_elsewhere_says_so(tmp_path):
    """A fault outside the snippets: the first install holds nothing back
    and leaves nothing behind, and the second nginx -t names the cause."""
    host = _Host(tmp_path)
    (host.conf / "site.conf").write_text("BROKEN;\n")
    r = host.run()
    assert r.returncode == 1
    assert not (host.dir / "http.conf").exists() and not (host.dir / "server.conf").exists()
    assert "nginx -t fails with that too: the fault is elsewhere in nginx's config" in r.stderr
    assert host.leftovers() == []


def test_a_gate_that_did_not_finish_puts_the_kept_file_back(tmp_path):
    host = _Host(tmp_path)
    assert host.run().returncode == 0
    good = (host.dir / "server.conf").read_text()
    # A run stopped between the staging and nginx -t.
    (host.dir / "server.conf.old").write_text(good)
    (host.dir / "server.conf").write_text("BROKEN;\n")
    with open(host.src / "nginx.conf", "a") as f:
        f.write("BROKEN;\n")
    host.calls()
    r = host.run()
    assert r.returncode == 1 and (host.dir / "server.conf").read_text() == good


def test_an_include_of_the_clone_gets_a_note_and_the_copies_still_go_in(tmp_path):
    host = _Host(tmp_path)
    sites = host.conf / "sites-enabled"
    sites.mkdir()
    site = f"server {{\n    listen 443 ssl;\n    include {host.src}/nginx.conf;\n}}\n"
    (sites / "relay").write_text(site)
    main = f"http {{\n    include mime.types;\n\tinclude {host.src}/nginx-http.conf;\n}}\n"
    (host.conf / "nginx.conf").write_text(main)
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert "nginx: these include lines name the clone, which each update replaces" in r.stdout
    assert f"  {sites}/relay:3:    include {host.src}/nginx.conf;" in r.stdout
    assert f"  {host.conf}/nginx.conf:3:\tinclude {host.src}/nginx-http.conf;" in r.stdout
    assert f"  include {host.dir}/http.conf;    # in the http block" in r.stdout
    assert f"  include {host.dir}/server.conf;  # in the HTTPS server block" in r.stdout
    # The operator's files are theirs; the copies go in all the same.
    assert (sites / "relay").read_text() == site and (host.conf / "nginx.conf").read_text() == main
    assert (host.dir / "server.conf").read_text() == (host.src / "nginx.conf").read_text()
    # Once nginx includes the copies, the run says nothing more.
    (sites / "relay").write_text(f"server {{\n    include {host.dir}/server.conf;\n}}\n")
    (host.conf / "nginx.conf").write_text(f"http {{\n    include {host.dir}/http.conf;\n}}\n")
    r = host.run()
    assert r.returncode == 0 and r.stdout.splitlines() == \
        [f"nginx: {host.dir}/http.conf and server.conf match this release; nginx left alone."]


def test_a_first_install_says_which_include_lines_to_add(tmp_path):
    host = _Host(tmp_path)
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert f"nginx: no include line names {host.dir} yet" in r.stdout
    assert f"  include {host.dir}/http.conf;    # in the http block" in r.stdout


def test_no_nginx_touches_nothing(tmp_path):
    host = _Host(tmp_path)
    r = host.run("off")
    assert r.returncode == 0, r.stderr
    assert not host.dir.exists(), "no file under nginx's config"
    assert host.calls() == [], "no nginx -t, no systemctl"
    assert "nginx: left alone (--no-nginx)." in r.stdout
    assert f"{host.src}/nginx-http.conf" in r.stdout and f"{host.dir}/server.conf" in r.stdout
    assert f"  include {host.dir}/server.conf;  # in the HTTPS server block" in r.stdout
    # Copies from an earlier run stay as they were, though the release changed.
    assert host.run().returncode == 0
    host.calls()
    os.utime(host.dir / "server.conf", (1_000_000_000, 1_000_000_000))
    with open(host.src / "nginx.conf", "a") as f:
        f.write("# a later release's line\n")
    r = host.run("off")
    assert r.returncode == 0 and host.calls() == []
    assert (host.dir / "server.conf").stat().st_mtime_ns == 1_000_000_000 * 10**9
    assert host.leftovers() == []


def test_without_nginx_the_copies_go_in_untested(tmp_path):
    host = _Host(tmp_path, nginx=False)
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert (host.dir / "http.conf").read_text() == (host.src / "nginx-http.conf").read_text()
    assert host.calls() == [], "no nginx to test or reload"
    assert f"nginx: not installed here; http.conf and server.conf written to {host.dir}" in r.stdout
    assert f"  include {host.dir}/http.conf;    # in the http block" in r.stdout


def test_a_failed_gate_exits_non_zero_after_the_relay_is_updated(tmp_path):
    """The script's last part (the relay's unit, the updater, nginx, the
    summary) on the fake host: a snippet that fails nginx -t keeps the
    previous file and makes the exit non-zero, and the relay's unit was
    written and restarted first."""
    host = _Host(tmp_path)
    host._shim("sleep", "#!/bin/bash\n")
    host._shim("python3", '#!/bin/bash\necho \'{"ok": true}\'\n')
    root = tmp_path / "root"
    base, etc, systemd = root / "opt", root / "etc", root / "systemd"
    for d in (base, etc, systemd):
        d.mkdir(parents=True)
    (etc / "installs.txt").write_text("a" * 26 + "\n")
    text = SCRIPT.read_text()
    tail = "# --- the unit: written here" + text.split("# --- the unit: written here", 1)[1]
    head = "\n".join(["set -euo pipefail", f"BASE={base}", f"ETC={etc}", f"CLONE={host.clone}",
                      f"UNIT={systemd}/tacitus-relay.service",
                      f"UPDATER={systemd}/tacitus-relay-update", f"NGINX_DIR={host.dir}",
                      f"NGINX_CONF={host.conf}", f"PY={host.bin}/python3", 'STATIC=""', "WITH_APP=0",
                      "UPDATE_TIMER=1", "NGINX=on", f"COMMIT={SHA}"])
    code = head + "\n" + _cut("nginx_snippets") + "\n" + tail

    def run():
        return subprocess.run([shutil.which("bash"), "-c", code], capture_output=True, text=True,
                              env=host.env(), timeout=60)

    r = run()
    assert r.returncode == 0, r.stderr
    assert f"Installed {SHA} under {base}." in r.stdout
    assert "systemctl reload nginx" in host.calls()
    old = (host.dir / "server.conf").read_text()
    (systemd / "tacitus-relay.service").unlink()
    with open(host.src / "nginx.conf", "a") as f:
        f.write("BROKEN;\n")
    r = run()
    assert r.returncode == 1
    calls = host.calls()
    # The relay's unit was written and restarted before nginx -t ran.
    unit = (systemd / "tacitus-relay.service").read_text()
    assert f"ExecStart={host.bin}/python3 -I {base}/relay_server.py --bind 127.0.0.1" in unit
    assert calls.index("systemctl restart tacitus-relay.service") < calls.index("nginx -t")
    assert "systemctl enable --now tacitus-relay-update.timer" in calls
    assert "systemctl reload nginx" not in calls
    assert (host.dir / "server.conf").read_text() == old
    assert "nginx -t failed with this release's server.conf" in r.stderr
    assert f"Installed {SHA} under {base}." in r.stdout, "the summary still prints"
