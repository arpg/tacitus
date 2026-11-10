"""A Codex turn's network tools run through the mediator as the resident.

A Codex turn runs its commands in a sandbox with no network. On a host
with the mediator, the tools that need the network and hold no owner
credential (mail_mediation.RESIDENT_NET: a page fetch, a booking link
read, the site, Overleaf and doc-review git steps) and a plugin's own
script (plugin_run.py) hand the call to the mediator, which runs it as
the resident outside the sandbox.

What must hold: the owner never runs one of these; the gated publish and
push verbs are not among them; only a Codex sandbox on a mediated host
forwards, and a Claude turn, the owner and the mediator's own child run
the tool in place; a ghost run forwards a page read only, and its other
calls are recorded; the child gets the resident's identity and an
environment built by the mediator; a plugin script runs only when a Claude
turn may run the same command line; a busy pool or a time limit answers 3
and says so; a forwarded site commit stages the Publish card; each client
forwards before any import a bare interpreter lacks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-resident-net-"))

import codex_ghost as cg  # noqa: E402
import codex_turn as ct  # noqa: E402
import mail_mediation as M  # noqa: E402
import mail_mediator as mm  # noqa: E402
import plugin_registry as pr  # noqa: E402

PLUGIN = "/srv/ea/plugins/trains/lookup.py"
GRANT = "Bash(python3 /srv/ea/plugins/trains/lookup.py *)"


@pytest.fixture
def sandbox(monkeypatch):
    """A command of a Codex turn's sandbox on a mediated host."""
    monkeypatch.setenv(M.SANDBOX_ENV, "1")
    monkeypatch.delenv(M.RESIDENT_DIRECT_ENV, raising=False)
    monkeypatch.setenv("EA_RUN_ID", "chat-run-1")
    monkeypatch.setattr(M, "should_mediate", lambda: True)


# ── the table ────────────────────────────────────────────────────────

def test_the_owner_runs_no_resident_net_tool():
    assert not set(M.RESIDENT_NET) & (set(M.SAFE) | set(M.GATED) | set(mm.TOOLS))
    assert set(mm.RESIDENT_NET_TOOLS) == set(M.RESIDENT_NET)
    assert M.PLUGIN_RUN_TOOL not in mm.TOOLS and M.PLUGIN_RUN_TOOL not in mm.RESIDENT_NET_TOOLS
    for tool, path in mm.RESIDENT_NET_TOOLS.items():
        assert path == ROOT / "preprocessor" / f"{tool}.py" and path.is_file()


def test_the_gated_and_local_verbs_are_not_in_the_table():
    # A publish and a push take the owner's one-time token; the bot runs them.
    assert "publish" not in M.RESIDENT_NET["site_tools"]
    assert "push" not in M.RESIDENT_NET["overleaf_tools"]
    assert not M.is_resident_net("site_tools", "publish")
    assert not M.is_resident_net("overleaf_tools", "push")
    assert not M.is_resident_net("booking_link", "book")
    assert not M.is_resident_net("graph_actions", "send-draft")
    assert M.is_resident_net("fetch_url", "fetch") and M.is_resident_net("doc_review", "open")
    assert M.is_resident_net(M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB)
    assert not M.is_resident_net(M.PLUGIN_RUN_TOOL, "status")
    assert M.is_allowed("fetch_url", "fetch", ["fetch"]) and not M.is_safe("fetch_url", "fetch")


def test_the_ghost_prefix_is_one_value_in_three_modules():
    assert M.GHOST_RUN_PREFIX == cg.RUN_PREFIX == mm._GHOST_PREFIX
    assert M.GHOST_RESIDENT_NET == {("fetch_url", "fetch")}


def test_the_time_limits_fit_inside_a_turn():
    assert M.RESIDENT_NET_TIMEOUT_S < M.PLUGIN_RUN_TIMEOUT_S <= ct.TURN_TIMEOUT_S
    assert M.RESIDENT_WAIT_MARGIN_S > mm.RESIDENT_TERM_GRACE_S


# ── who forwards ─────────────────────────────────────────────────────

def test_only_a_codex_sandbox_on_a_mediated_host_forwards(sandbox, monkeypatch):
    assert M.resident_net_applies("fetch_url", "fetch")
    assert M.resident_net_applies("site_tools", "commit")
    assert M.resident_net_applies(M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB)
    assert not M.resident_net_applies("site_tools", "publish")
    assert not M.resident_net_applies("site_tools", "add-file")
    # The mediator's child is that same call, run in place.
    with monkeypatch.context() as box:
        box.setenv(M.RESIDENT_DIRECT_ENV, "1")
        assert not M.in_net_sandbox() and not M.resident_net_applies("fetch_url", "fetch")
    # A Claude turn, the owner's shell, a cron: no sandbox variable.
    with monkeypatch.context() as box:
        box.delenv(M.SANDBOX_ENV)
        assert not M.resident_net_applies("fetch_url", "fetch")
    # No mediator: the tool runs in the sandbox, on its cache or staged.
    with monkeypatch.context() as box:
        box.setattr(M, "should_mediate", lambda: False)
        assert not M.resident_net_applies("fetch_url", "fetch")
        assert M.sandbox_request("fetch_url", ["fetch", "--url", "https://a.example/"]) is None
        assert M.sandbox_forward("fetch_url", ["fetch", "--url", "https://a.example/"]) is None


def test_a_ghost_run_forwards_a_page_read_and_nothing_else(sandbox, monkeypatch):
    monkeypatch.setenv("EA_RUN_ID", cg.RUN_PREFIX + "20260101-chat")
    assert M.resident_net_applies("fetch_url", "fetch")
    for tool, verb in (("site_tools", "commit"), ("site_tools", "status"), ("overleaf_tools", "pull"),
                       ("doc_review", "open"), ("booking_link", "read"),
                       (M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB)):
        assert not M.resident_net_applies(tool, verb), (tool, verb)


def test_a_forward_carries_the_working_directory_and_waits_past_the_limit(sandbox, monkeypatch, tmp_path):
    seen = []

    def request(tool, verb, argv, *, wait_s, extra):
        seen.append((tool, verb, list(argv), wait_s, extra))
        return {"stdout": "page", "stderr": "", "rc": 0}

    monkeypatch.setattr(M, "_request", request)
    monkeypatch.chdir(tmp_path)
    assert M.sandbox_request("fetch_url", ["fetch", "--url", "https://a.example/"])["stdout"] == "page"
    M.sandbox_request(M.PLUGIN_RUN_TOOL, [M.PLUGIN_RUN_VERB, PLUGIN, "--from", "A"])
    assert seen[0] == ("fetch_url", "fetch", ["fetch", "--url", "https://a.example/"],
                       M.RESIDENT_NET_TIMEOUT_S + M.RESIDENT_WAIT_MARGIN_S, {"cwd": str(tmp_path)})
    assert seen[1][3] == M.PLUGIN_RUN_TIMEOUT_S + M.RESIDENT_WAIT_MARGIN_S
    assert seen[1][2] == [M.PLUGIN_RUN_VERB, PLUGIN, "--from", "A"]


def test_a_refused_call_and_a_silent_mediator_answer_as_the_tool_would(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(M, "_request", lambda *a, **kw: pytest.fail("a refused call is never queued"))
    r = M.sandbox_request("fetch_url", ["fetch", "--url", "https://a.example/", "--no-such-option"])
    assert r["rc"] == 2 and "BLOCKED" in r["stderr"] and "--no-such-option" in r["stderr"]
    r = M.sandbox_request("site_tools", ["commit", "--site", "s", "--site", "t", "--message", "m"])
    assert r["rc"] == 2 and "BLOCKED" in r["stderr"]
    monkeypatch.setattr(M, "_request", lambda *a, **kw: None)
    r = M.sandbox_request("fetch_url", ["fetch", "--url", "https://a.example/"])
    assert r["rc"] == 3 and "did not respond in time" in r["stderr"]
    with pytest.raises(SystemExit) as stop:
        M.sandbox_forward("fetch_url", ["fetch", "--url", "https://a.example/"])
    assert stop.value.code == 3 and "did not respond in time" in capsys.readouterr().err
    monkeypatch.setattr(M, "_request", lambda *a, **kw: {"stdout": "out\n", "stderr": "warn\n", "rc": 5})
    with pytest.raises(SystemExit) as stop:
        M.sandbox_forward("overleaf_tools", ["status", "--dir", "/x"])
    got = capsys.readouterr()
    assert stop.value.code == 5 and got.out == "out\n" and got.err == "warn\n"


def test_the_shape_of_a_plugin_run_and_of_a_working_directory():
    ok = [M.PLUGIN_RUN_VERB, PLUGIN, "--from", "A"]
    assert M.plugin_run_error(M.PLUGIN_RUN_VERB, ok) is None
    assert M.sanitize(M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, ok) is None
    for verb, argv in (("status", ["status", PLUGIN]), (M.PLUGIN_RUN_VERB, []),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB]),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, "lookup.py"]),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, "/srv/ea/plugins/trains/lookup.sh"]),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, "/srv/ea/plugins/../x/lookup.py"]),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN, "a\x00b"]),
                       (M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN, 3])):
        assert M.plugin_run_error(verb, argv), argv
    for cwd in (None, "", "/home/resident/.ea/state/codex-live"):
        assert M.cwd_error(cwd) is None
    for cwd in ("state", "/a/../b", "/a\nb", "/a\x00b", 7, "/" + "a" * 1100):
        assert M.cwd_error(cwd), cwd


# ── the mediator's child ─────────────────────────────────────────────

@pytest.fixture
def mediator(monkeypatch, tmp_path):
    """The mediator's handler with its answers, audit lines and child
    processes captured. `box.child` is what the next child does."""
    box = types.SimpleNamespace(answers={}, audit=[], cmds=[], kw=[], threads=[], recorded=[],
                                child=lambda p, timeout: ("out\n", "", 0), signals=[])
    queue = tmp_path / "mail-queue"
    queue.mkdir()
    monkeypatch.setattr(M, "QUEUE", queue)
    monkeypatch.setattr(mm.resident_io, "read_text", lambda p: Path(p).read_text())
    monkeypatch.setattr(mm, "_respond", lambda rid, out, err, rc: box.answers.__setitem__(
        rid, {"stdout": out, "stderr": err, "rc": rc}))
    monkeypatch.setattr(mm, "_audit", lambda tool, verb, outcome: box.audit.append((tool, verb, outcome)))
    monkeypatch.setattr(mm.pwd, "getpwnam", lambda name: types.SimpleNamespace(
        pw_dir="/home/resident", pw_uid=1999, pw_gid=1999))
    monkeypatch.setattr(mm, "RESIDENT_TERM_GRACE_S", 0)
    monkeypatch.setattr(mm, "_resident_slots", mm.threading.BoundedSemaphore(mm.RESIDENT_NET_MAX))

    class Popen:
        def __init__(self, cmd, **kw):
            box.cmds.append(list(cmd))
            box.kw.append(kw)
            self.returncode = None

        def communicate(self, timeout=None):
            out, err, rc = box.child(self, timeout)
            self.returncode = rc
            return out, err

        def terminate(self):
            box.signals.append("TERM")

        def kill(self):
            box.signals.append("KILL")

    monkeypatch.setattr(mm.subprocess, "Popen", Popen)

    class Thread:
        def __init__(self, target, daemon=None):
            self.target = target
            box.threads.append(self)

        def start(self):
            self.target()

    monkeypatch.setattr(mm.threading, "Thread", Thread)
    monkeypatch.setattr(pr, "plugin_tool_grants", lambda plugins_dir=None, lock=None: [GRANT])
    monkeypatch.setattr(pr, "denied_tools", lambda lock=None: [])
    monkeypatch.setattr(cg, "record_write", lambda run_id, tool, verb, argv, **kw: box.recorded.append(
        (run_id, tool, verb, list(argv))))

    def send(rid, tool, verb, argv, **extra):
        req = queue / f"req-{rid}.json"
        req.write_text(json.dumps({"tool": tool, "verb": verb, "argv": argv, **extra}))
        mm.handle(str(req))
        assert not req.exists(), "every request file is removed"
        return box.answers[rid]

    box.send = send
    return box


def _env_of(cmd: list[str]) -> dict:
    """The NAME=value words between `env -i` and the command."""
    i = cmd.index("-i") + 1
    out = {}
    while "=" in cmd[i] and not cmd[i].startswith("/"):
        name, _, value = cmd[i].partition("=")
        out[name] = value
        i += 1
    return out


def test_a_core_tool_runs_as_the_resident_with_an_environment_built_here(mediator, monkeypatch):
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", "owner-only")
    r = mediator.send("a1", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"],
                      cwd="/home/resident/.ea/state/codex-live", run_id="chat-run-1", model="codex",
                      env={"LD_PRELOAD": "/x.so"})
    assert r == {"stdout": "out\n", "stderr": "", "rc": 0}
    cmd = mediator.cmds[0]
    assert cmd[:6] == ["sudo", "-n", "-u", M.RESIDENT_USER, "env", "-i"]
    env = _env_of(cmd)
    assert env == {"HOME": "/home/resident", "PATH": mm._resident_path("/home/resident"),
                   "EA_DIR": str(M.EA_DIR.resolve()), "EA_BOT_HOME": "/home/resident",
                   M.RESIDENT_DIRECT_ENV: "1", M.MEDIATED_VERB_ENV: "fetch",
                   "EA_RUN_ID": "chat-run-1", "EA_TURN_MODEL": "codex"}
    tail = cmd[6 + len(env):]
    assert tail == ["sh", "-c", 'cd -- "$1" && shift && exec "$@"', "sh",
                    "/home/resident/.ea/state/codex-live", "python3",
                    str(ROOT / "preprocessor" / "fetch_url.py"), "fetch", "--url", "https://a.example/"]
    # The owner's environment does not reach sudo either.
    assert mediator.kw[0]["env"] == {"PATH": "/usr/bin:/bin"}
    assert "owner-only" not in json.dumps(cmd) and "LD_PRELOAD" not in json.dumps(cmd)
    assert mediator.audit == [("fetch_url", "fetch", "rc=0")]


def test_a_run_id_that_is_not_plain_and_a_missing_directory_are_left_out(mediator):
    mediator.send("a2", "site_tools", "status", ["status", "--site", "notes"], run_id="x y;z")
    cmd = mediator.cmds[0]
    env = _env_of(cmd)
    assert "EA_RUN_ID" not in env and "EA_TURN_MODEL" not in env
    assert cmd[6 + len(env):] == ["python3", str(ROOT / "preprocessor" / "site_tools.py"),
                                  "status", "--site", "notes"]


def test_the_child_s_status_and_output_are_the_answer(mediator):
    mediator.child = lambda p, timeout: ("", '{"status": "error"}\n', 1)
    r = mediator.send("a3", "overleaf_tools", "pull", ["pull", "--dir", "/home/resident/proposals/p"])
    assert r == {"stdout": "", "stderr": '{"status": "error"}\n', "rc": 1}
    assert mediator.audit == [("overleaf_tools", "pull", "rc=1")]


def test_a_gated_or_unknown_verb_of_a_listed_tool_is_refused(mediator):
    for rid, tool, verb, argv in (
            ("b1", "site_tools", "publish", ["publish", "--site", "s", "--commit", "c", "--base", "b"]),
            ("b2", "overleaf_tools", "push", ["push", "--dir", "/d", "--commit", "c", "--base", "b", "--project", "p"]),
            ("b3", "booking_link", "book", ["book"]),
            ("b4", "fetch_url", "fetch", ["--url", "https://a.example/"]),
            ("b5", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/", "--save-imag", "/x.png"]),
            ("b6", M.PLUGIN_RUN_TOOL, "status", ["status", PLUGIN])):
        r = mediator.send(rid, tool, verb, argv)
        assert r["rc"] == 2 and "mediator refused" in r["stderr"], rid
    assert mediator.cmds == []


def test_a_bad_working_directory_is_refused(mediator):
    r = mediator.send("c1", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"], cwd="state/../..")
    assert r["rc"] == 2 and "working directory" in r["stderr"]
    assert mediator.cmds == [] and mediator.audit[-1][2] == "REFUSED-PATH"


def test_a_ghost_run_s_write_is_recorded_and_its_page_read_runs(mediator):
    run_id = cg.RUN_PREFIX + "20260101-chat"
    r = mediator.send("d1", "site_tools", "commit", ["commit", "--site", "notes", "--message", "m"], run_id=run_id)
    assert r["rc"] == 0 and json.loads(r["stdout"]) == {
        "status": "recorded", "ghost": True,
        "message": "ghost run: 'site_tools commit' recorded, not executed"}
    assert mediator.recorded == [(run_id, "site_tools", "commit", ["commit", "--site", "notes", "--message", "m"])]
    r = mediator.send("d2", M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN], run_id=run_id)
    assert json.loads(r["stdout"])["ghost"] is True
    assert mediator.cmds == []
    r = mediator.send("d3", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"], run_id=run_id)
    assert r["stdout"] == "out\n" and len(mediator.cmds) == 1
    assert [a[2] for a in mediator.audit] == ["GHOST-RECORDED", "GHOST-RECORDED", "rc=0"]


def test_a_plugin_script_runs_only_when_a_claude_turn_may_run_the_same_call(mediator, monkeypatch):
    r = mediator.send("e1", M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN, "--from", "A"],
                      cwd="/home/resident/.ea/state/codex-live")
    assert r["rc"] == 0
    cmd = mediator.cmds[0]
    env = _env_of(cmd)
    # A plugin's parser is its own: no verb is named for it to confirm.
    assert M.MEDIATED_VERB_ENV not in env and env[M.RESIDENT_DIRECT_ENV] == "1"
    assert cmd[-4:] == ["python3", PLUGIN, "--from", "A"]
    assert mediator.audit[-1] == (M.PLUGIN_RUN_TOOL, "lookup.py", "rc=0")
    # No grant: a script outside an enabled plugin, the plugin's other file.
    for rid, script in (("e2", "/home/resident/.ea/state/own.py"), ("e3", "/srv/ea/plugins/trains/admin.py")):
        r = mediator.send(rid, M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, script])
        assert r["rc"] == 2 and "not a granted plugin script call" in r["stderr"]
        assert mediator.audit[-1][2] == "REFUSED-PLUGIN"
    # A denial outranks the grant, as it does in a Claude turn.
    monkeypatch.setattr(pr, "denied_tools", lambda lock=None: [
        "Bash(python3 /srv/ea/plugins/trains/lookup.py --delete *)"])
    r = mediator.send("e4", M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN, "--delete", "x"])
    assert r["rc"] == 2 and "deny list forbids" in r["stderr"]
    # Grants that cannot be read refuse the call.
    monkeypatch.setattr(pr, "plugin_tool_grants", lambda **kw: (_ for _ in ()).throw(OSError("lock unreadable")))
    r = mediator.send("e5", M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN])
    assert r["rc"] == 2 and "could not be read" in r["stderr"]
    assert len(mediator.cmds) == 1


def test_permission_rules_match_a_command_line_as_the_claude_lane_reads_them():
    assert pr.bash_rule_matches(GRANT, f"python3 {PLUGIN} --from A")
    assert pr.bash_rule_matches(GRANT, f"python3 {PLUGIN}"), "a rule that ends in ' *' takes the bare command"
    assert not pr.bash_rule_matches(GRANT, f"python3 {PLUGIN}x --from A")
    assert not pr.bash_rule_matches(GRANT, f"python3 /srv/ea/plugins/trains/sub/lookup.py")
    assert not pr.bash_rule_matches("Edit(//srv/**)", f"python3 {PLUGIN}")
    assert pr.bash_rule_matches("Bash(python3 *lookup.py --delete *)", f"python3 {PLUGIN} --delete x")
    assert not pr.bash_rule_matches(f"Bash(python3 {PLUGIN})", f"python3 {PLUGIN} --from A")
    core = pr.core_denied_tools()
    assert core and all(not r.startswith("#") for r in core)
    assert pr.script_call_error(PLUGIN, ["--from", "A"], lock={"plugins": {}}) is not None


def test_a_full_pool_answers_busy_and_starts_nothing(mediator):
    for _ in range(mm.RESIDENT_NET_MAX):
        assert mm._resident_slots.acquire(blocking=False)
    r = mediator.send("f1", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"])
    assert r["rc"] == 3 and "busy; no action started" in r["stderr"]
    assert mediator.cmds == [] and mediator.audit[-1][2] == "BUSY"
    mm._resident_slots.release()
    r = mediator.send("f2", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"])
    assert r["rc"] == 0
    # The slot comes back after the child ends.
    assert mm._resident_slots.acquire(blocking=False)


def test_a_child_past_its_time_limit_is_stopped_and_the_answer_says_so(mediator):
    calls = []

    def slow(p, timeout):
        calls.append(timeout)
        if len(calls) <= 2:
            raise subprocess.TimeoutExpired("child", timeout)
        return "", "", -9

    mediator.child = slow
    r = mediator.send("g1", "site_tools", "pull", ["pull", "--site", "notes"])
    assert r["rc"] == 3 and json.loads(r["stderr"])["message"] == (
        f"'site_tools pull' was stopped after {M.RESIDENT_NET_TIMEOUT_S} s. Its state is unknown: "
        f"check before any retry.")
    # SIGTERM first, which sudo passes on to the child; SIGKILL after the grace.
    assert calls[0] == M.RESIDENT_NET_TIMEOUT_S and mediator.signals == ["TERM", "KILL"]
    assert mediator.audit[-1] == ("site_tools", "pull", "EXEC-TIMEOUT")
    calls.clear()
    mediator.send("g2", M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN])
    assert calls[0] == M.PLUGIN_RUN_TIMEOUT_S
    assert mm._resident_slots.acquire(blocking=False), "the slot comes back after a stopped child"


def test_a_child_that_cannot_start_answers_3(mediator, monkeypatch):
    def no_sudo(cmd, **kw):
        raise FileNotFoundError("sudo")

    monkeypatch.setattr(mm.subprocess, "Popen", no_sudo)
    r = mediator.send("h1", "fetch_url", "fetch", ["fetch", "--url", "https://a.example/"])
    assert r["rc"] == 3 and "could not run 'fetch_url fetch'" in r["stderr"]
    assert mediator.audit[-1][2] == "EXEC-ERR"


# ── the tools ────────────────────────────────────────────────────────

def test_fetch_url_takes_its_old_command_line_and_the_verb():
    import fetch_url
    assert fetch_url._cli_argv(["--url", "https://a.example/"]) == ["fetch", "--url", "https://a.example/"]
    assert fetch_url._cli_argv(["fetch", "--url", "https://a.example/"]) == ["fetch", "--url", "https://a.example/"]
    args = fetch_url._parser().parse_args(fetch_url._cli_argv(["--url", "https://a.example/", "--js"]))
    assert args.command == "fetch" and args.js and args.max_chars == 4000
    assert M.sanitize("fetch_url", "fetch", ["fetch", "--url", "https://a.example/", "--max-chars", "900"]) is None


def test_a_saved_image_stays_in_the_folders_a_claude_turn_may_use(tmp_path, monkeypatch):
    # The child is the resident outside the sandbox: the tool's own check
    # is what holds a path option, here and in a Claude turn alike.
    import fetch_url
    monkeypatch.setattr(fetch_url, "_save_roots", lambda: [tmp_path / "state"])
    (tmp_path / "state").mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(fetch_url, "validate_url", lambda url: pytest.fail("refused before any fetch"))
    r = fetch_url.save_image("https://a.example/p.png", str(tmp_path / "elsewhere" / "p.png"))
    assert "outside the folders an image may be saved in" in r["error"] and r["saved"] is None


def test_an_overleaf_folder_outside_the_clone_roots_is_refused(tmp_path, monkeypatch):
    import overleaf_tools as ot
    monkeypatch.setattr(ot, "roots", lambda: [tmp_path / "proposals"])
    (tmp_path / "proposals").mkdir()
    with pytest.raises(ot.OverleafError):
        ot._resolve(str(tmp_path / "elsewhere"))
    with pytest.raises(ot.OverleafError):
        ot._resolve("proposals/p")
    assert ot._resolve(str(tmp_path / "proposals" / "p")) == (tmp_path / "proposals" / "p").resolve()


def test_an_empty_git_folder_is_not_listed_as_an_overleaf_clone(tmp_path, monkeypatch):
    # A Codex sandbox keeps an empty .git in each folder it may write.
    import overleaf_tools as ot
    root = tmp_path / "proposals"
    (root / ".git").mkdir(parents=True)
    (root / "real" / ".git").mkdir(parents=True)
    (root / "real" / ".git" / "config").write_text("")
    monkeypatch.setattr(ot, "roots", lambda: [root])
    monkeypatch.setattr(ot, "_config_entries", lambda clone: [
        ("remote.origin.url", "https://git.overleaf.com/0123456789abcdef01234567")])
    listed = json.dumps(ot.cmd_list())
    assert str(root / "real") in listed and f'"{root}"' not in listed


def test_a_forwarded_site_commit_stages_the_publish_card(tmp_path, monkeypatch, capsys):
    import publish_gate as gate
    import site_tools as st
    monkeypatch.setattr(st, "SITES", {"notes": {"title": "N", "repo": "o/n", "url": "https://n.example/"}})
    monkeypatch.setattr(st, "EA_DIR", tmp_path)
    (tmp_path / "state").mkdir()
    import preferences
    import resident_io
    # Outside the sandbox the clone's .git is writable, so nothing is left
    # for the bot to replay.
    monkeypatch.setattr(st, "_clone_git_writable", lambda slug: True)
    monkeypatch.setattr(preferences, "stage", lambda *a, **kw: pytest.fail("the resident child never stages"))
    request = {"site": "notes", "commit": "c" * 40}
    monkeypatch.setattr(st, "cmd_commit", lambda slug, message: {
        "status": "ok", "commit": "c" * 40, "stage": {"request": request}})
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    staged = tmp_path / "state" / gate.STAGE_FILE
    # In the mediator's child the commit made outside the sandbox stages the card.
    monkeypatch.setenv(M.MEDIATED_VERB_ENV, "commit")
    assert st.main(["commit", "--site", "notes", "--message", "Fix a date"]) == 0
    assert json.loads(capsys.readouterr().out)["publish_staged"] is True
    assert json.loads(staged.read_text()) == request
    assert M.MEDIATED_VERB_ENV not in os.environ, "a process the child starts is not held to its verb"
    # A Claude turn's own commit answers the stage for the turn to write.
    staged.unlink()
    assert st.main(["commit", "--site", "notes", "--message", "Fix a date"]) == 0
    assert "publish_staged" not in json.loads(capsys.readouterr().out) and not staged.exists()
    # The child of one verb refuses another.
    monkeypatch.setenv(M.MEDIATED_VERB_ENV, "status")
    with pytest.raises(SystemExit) as stop:
        st.main(["commit", "--site", "notes", "--message", "Fix a date"])
    assert stop.value.code == 2 and not staged.exists()


def test_doc_review_reads_refresh_the_copy_through_the_mediator(sandbox, monkeypatch):
    import doc_review as dr
    seen = []

    def request(tool, verb, argv, *, wait_s, extra):
        seen.append((tool, list(argv)))
        return {"stdout": "", "stderr": "", "rc": 0}

    monkeypatch.setattr(M, "_request", request)
    assert dr._refresh_cache(M, 12) is None
    assert seen == [("doc_review", ["prefetch", "--pr", "12"])]
    monkeypatch.setattr(M, "_request", lambda *a, **kw: {"stdout": "", "stderr": "no such pull request\n", "rc": 1})
    assert dr._refresh_cache(M, 12)["rc"] == 1
    # No sandbox: the copy the bot prefetched stands.
    monkeypatch.delenv(M.SANDBOX_ENV)
    monkeypatch.setattr(M, "_request", lambda *a, **kw: pytest.fail("nothing forwards outside the sandbox"))
    assert dr._refresh_cache(M, 12) is None


def test_plugin_run_forwards_from_the_sandbox_and_runs_in_place_elsewhere(sandbox, monkeypatch, capsys):
    import plugin_run
    seen = []

    def request(tool, verb, argv, *, wait_s, extra):
        seen.append((tool, verb, list(argv)))
        return {"stdout": "found\n", "stderr": "", "rc": 0}

    monkeypatch.setattr(M, "_request", request)
    with pytest.raises(SystemExit) as stop:
        plugin_run.main([PLUGIN, "--from", "A"])
    assert stop.value.code == 0 and capsys.readouterr().out == "found\n"
    assert seen == [(M.PLUGIN_RUN_TOOL, M.PLUGIN_RUN_VERB, [M.PLUGIN_RUN_VERB, PLUGIN, "--from", "A"])]
    with pytest.raises(SystemExit) as stop:
        plugin_run.main(["/srv/ea/plugins/trains/notes.txt"])
    assert stop.value.code == 2 and "not an absolute path of a .py script" in capsys.readouterr().err
    # Outside the sandbox the script takes this process's place.
    monkeypatch.delenv(M.SANDBOX_ENV)
    ran = []
    monkeypatch.setattr(plugin_run.os, "execv", lambda exe, argv: ran.append((exe, argv)))
    plugin_run.main([PLUGIN, "--from", "A"])
    assert ran == [(sys.executable, [sys.executable, PLUGIN, "--from", "A"])]
    assert plugin_run.main([]) == 2


def test_no_folder_a_codex_turn_may_write_holds_plugin_code(tmp_path, monkeypatch):
    # plugin_run gives a script the network. The sandbox cannot change one:
    # of a plugin's folder only its state is a write root, and no grant
    # names a file under a state folder.
    ea, home = tmp_path / "ea", tmp_path / "home"
    for d in ("state", "docs", "logs", "config", "plugins/trains/state", "plugins/trains/lib"):
        (ea / d).mkdir(parents=True)
    (ea / "plugins" / "trains" / "lookup.py").write_text("")
    (ea / "plugins" / "trains" / "state" / "cache.py").write_text("")
    home.mkdir()
    monkeypatch.setattr(ct, "REAL_EA_DIR", ea)
    monkeypatch.setattr(ct, "BOT_HOME", home)
    roots = ct.live_write_paths()
    plugins = ea / "plugins"
    assert ea / "plugins" / "trains" / "state" in roots
    for root in roots:
        assert root != plugins and plugins not in root.parents or "state" in root.relative_to(plugins).parts
        assert root not in (ea, ea / "plugins" / "trains", ea / "config")
    grants = pr.expand_tool_grants([pr.V1_CHAT_TEMPLATE], ea / "plugins" / "trains",
                                   ["lookup.py", "state/cache.py"])
    assert any("lookup.py" in g for g in grants) and not any("cache.py" in g for g in grants)


CLIENT_REFUSALS = [
    ("fetch_url", ["--url", "https://a.example/", "--no-such-option"], "--no-such-option"),
    ("booking_link", ["read", "--no-such-option"], "--no-such-option"),
    ("site_tools", ["status", "--site", "notes", "--no-such-option"], "--no-such-option"),
    ("overleaf_tools", ["status", "--dir", "/x", "--no-such-option"], "--no-such-option"),
    ("doc_review", ["status", "--no-such-option"], "--no-such-option"),
    ("plugin_run", ["/srv/ea/plugins/trains/notes.txt"], "not an absolute path of a .py script"),
]


@pytest.mark.parametrize("tool,argv,why", CLIENT_REFUSALS, ids=[c[0] for c in CLIENT_REFUSALS])
def test_each_client_forwards_before_any_import_a_bare_interpreter_lacks(tool, argv, why):
    # `python3 -S` has no site-packages, as a sandbox's own python3 may not.
    # The refusal comes from the forward, before anything is queued.
    home = tempfile.mkdtemp(prefix="ea-test-bare-net-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
           "EA_DIR": str(Path(home) / ".ea"), "EA_FORCE_MEDIATE": "1", M.SANDBOX_ENV: "1"}
    r = subprocess.run([sys.executable, "-S", str(ROOT / "preprocessor" / f"{tool}.py"), *argv],
                       env=env, capture_output=True, text=True, timeout=60)
    assert "ModuleNotFoundError" not in r.stderr and "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 2 and why in r.stderr, r.stderr
    assert not (Path(home) / ".ea" / "state" / "mail-queue").exists()


def test_every_listed_tool_is_one_of_the_clients_checked_above():
    assert {c[0] for c in CLIENT_REFUSALS} == set(M.RESIDENT_NET) | {M.PLUGIN_RUN_TOOL}
