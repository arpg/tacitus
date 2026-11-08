"""push_notify: the owner-private settings, their validation, the request
it builds, and the report command. A fake opener stands in for ntfy;
nothing reaches the network or the real private directory."""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import push_notify as pn  # noqa: E402


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.delenv(pn.TOKEN_NAME, raising=False)
    return d


def _write(d, **settings):
    owner_trust.write_owner_file(d / pn.SETTINGS_FILE, json.dumps(settings).encode(), mode=0o600)


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, status=200):
        self.reqs, self.status = [], status

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        return _Resp(self.status)


GOOD = {"ntfy_url": "https://ntfy.example.org", "topic": "ea-a8f3k2m9q4w7z1x5"}


def test_no_settings_file_means_off(priv):
    assert pn.load_settings() is None
    assert pn.push("morning-briefing", "text", opener=Opener()) == "off"


def test_settings_are_never_read_outside_an_owner_process(priv, monkeypatch):
    _write(priv, **GOOD)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    assert pn.load_settings() is None


def test_validation_requires_https_or_a_local_server():
    assert pn.problems(GOOD) == []
    assert pn.problems({**GOOD, "ntfy_url": "http://ntfy.example.org"})
    assert pn.problems({**GOOD, "ntfy_url": "http://localhost:8080"}) == []
    assert pn.problems({**GOOD, "ntfy_url": "ftp://x"})
    assert pn.problems({"ntfy_url": GOOD["ntfy_url"]}) == ["topic is missing"]
    assert pn.problems({**GOOD, "topic": "a/b"})
    assert pn.problems({**GOOD, "click": "javascript:alert(1)"})
    assert pn.warnings({**GOOD, "topic": "short"})
    assert pn.warnings({**GOOD, "include_text": True})
    assert pn.warnings(GOOD) == []


def test_default_push_carries_no_report_text(priv):
    _write(priv, **GOOD)
    op = Opener()
    assert pn.push("morning-briefing", "Reply to the dean about the budget.", opener=op) == "sent"
    req = op.reqs[0]
    assert req.full_url == "https://ntfy.example.org/ea-a8f3k2m9q4w7z1x5"
    assert req.get_method() == "POST"
    assert b"dean" not in req.data
    assert req.get_header("Title") == "EA: Morning briefing"
    assert req.get_header("Authorization") is None


def test_include_text_sends_the_report_cut_to_the_limit(priv, monkeypatch):
    _write(priv, **GOOD, include_text=True, click="https://board.example.org/")
    monkeypatch.setenv(pn.TOKEN_NAME, "tok-123")
    op = Opener()
    pn.push("weekly-audit", "x" * 10000, opener=op)
    req = op.reqs[0]
    assert len(req.data) <= pn.MAX_TEXT_BYTES + 8
    assert req.get_header("Click") == "https://board.example.org/"
    assert req.get_header("Authorization") == "Bearer tok-123"
    assert req.get_header("Markdown") == "yes"


def test_invalid_settings_do_not_send(priv):
    _write(priv, ntfy_url="http://evil.example", topic="t")
    op = Opener()
    assert pn.push("morning-briefing", "text", opener=op).startswith("invalid")
    assert op.reqs == []


def test_a_failed_send_raises(priv):
    _write(priv, **GOOD)
    with pytest.raises(RuntimeError):
        pn.push("morning-briefing", "text", opener=Opener(status=500))


def test_report_command_is_quiet_without_settings(priv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("report text"))
    assert pn.main(["report", "--agent", "morning-briefing"]) == 0
    assert "off" in capsys.readouterr().out


def test_check_command_reports_problems(priv, capsys):
    _write(priv, ntfy_url="http://ntfy.example.org", topic="ea-a8f3k2m9q4w7z1x5")
    assert pn.main(["check"]) == 1
    assert "https" in capsys.readouterr().out


def test_group_writable_settings_file_is_refused(priv, capsys):
    p = priv / pn.SETTINGS_FILE
    p.write_text(json.dumps(GOOD))
    p.chmod(0o666)
    assert pn.main(["check"]) == 1
    assert "refused" in capsys.readouterr().err


def test_title_for_agent_names():
    assert pn.title_for("afternoon-catchup") == "EA: Afternoon catchup"
    assert pn.title_for("") == "EA: report"


def test_run_agent_pushes_only_on_the_no_chat_branch():
    text = (Path(__file__).resolve().parents[1] / "run-agent.sh").read_text()
    no_chat = text.split('log "Mode $PROFILE has no chat; the report file is the delivery."', 1)[1]
    assert 'push_notify.py" report' in no_chat.split("\n    fi", 1)[0]
    assert text.count('push_notify.py" report') == 1
