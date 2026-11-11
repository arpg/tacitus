"""remote_llm: the switchboard (config, end date, keep-from-Anthropic list), the
private store, and the client against a fake endpoint. Nothing here
reaches the network or the real private dir."""
import io
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402



@pytest.fixture(autouse=True)
def _local_chat_ready(monkeypatch):
    """The scheduling agent's live mode is the local model answering a
    scheduling turn, which the chat floor stops; these tests cover the
    mode itself, so the floor is lifted (tests/test_routing_presets_floor.py
    covers the floor)."""
    import ea_config
    monkeypatch.setattr(ea_config, "local_chat_ready", lambda *a, **k: True)

@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    return d


def _write_cfg(d, **over):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1",
           "mail": "shadow", "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil", "contractor.example.com"], "addresses": ["user@y.edu"]}}
    cfg.update(over)
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)


def test_off_outside_owner_process(priv, monkeypatch):
    _write_cfg(priv)
    monkeypatch.setattr(rl, "_owner", lambda: False)
    cfg = rl.config()
    assert cfg["mail"] == "off" and cfg["endpoint"] == ""
    with pytest.raises(rl.RemoteLLMError):
        rl.chat([{"role": "user", "content": "hi"}], purpose="t")


def test_missing_or_bad_config_is_off(priv):
    assert rl.config()["mail"] == "off"
    _write_cfg(priv, endpoint="http://plain.example/v1")
    assert rl.mode("mail") == "off"
    _write_cfg(priv, mail="bogus")
    assert rl.mode("mail") == "off"


def test_enforce_and_sealed_have_no_end_date(priv):
    _write_cfg(priv, mail="enforce", scheduling="enforce", shadow_until="2000-01-01")
    assert rl.mode("mail") == "enforce"
    assert rl.mode("scheduling") == "enforce"
    assert rl.enforcing("mail") and rl.enforcing("scheduling")
    _write_cfg(priv, mail="sealed", scheduling="sealed", shadow_until="2000-01-01")
    assert rl.mode("mail") == "sealed"
    assert rl.enforcing("mail") and rl.reading("mail")
    assert rl.mode("scheduling") == "off"
    assert not rl.enforcing("scheduling")
    _write_cfg(priv, mail="off", scheduling="shadow", shadow_until="2000-01-01")
    assert not rl.reading("mail") and not rl.reading("scheduling")
    _write_cfg(priv, mail="ghost", ghost={"mail": {"from": "2026-10-12", "until": "2026-10-18"}})
    assert rl.mode("mail", today=date(2026, 10, 18)) == "ghost"
    assert rl.mode("mail", today=date(2026, 10, 19)) == "enforce"
    assert rl.enforcing("mail") and rl.ghost_window("mail") == (date(2026, 10, 12), date(2026, 10, 18))


def test_shadow_needs_an_end_date_and_stops_after_it(priv):
    _write_cfg(priv, shadow_until="2026-10-26")
    cfg = rl.config()
    assert rl.mode("mail", cfg, today=date(2026, 10, 26)) == "shadow"
    assert rl.mode("mail", cfg, today=date(2026, 10, 27)) == "off"
    assert rl.mode("scheduling", cfg, today=date(2026, 10, 20)) == "off"
    _write_cfg(priv, shadow_until="")
    assert rl.mode("mail") == "off"


def test_keep_list_domains_cover_subdomains(priv):
    _write_cfg(priv)
    cfg = rl.config()
    assert rl.keep_reason("someone@army.mil", cfg) == "listed domain mil"
    assert rl.keep_reason("someone@us.af.mil", cfg)
    assert rl.keep_reason("someone@contractor.example.com", cfg)
    assert rl.keep_reason("USER@Y.EDU", cfg) == "listed address"
    assert rl.keep_reason("someone@example.edu", cfg) == ""
    assert rl.keep_reason("someone@notmil.com", cfg) == ""
    assert rl.keep_reason("", cfg) == ""


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(payload, seen):
    def open_fn(req, timeout=None):
        seen.append(json.loads(req.data))
        return _Resp(json.dumps(payload).encode())
    return open_fn


def test_chat_sends_schema_and_logs_no_text(priv):
    _write_cfg(priv)
    seen = []
    out = {"model": "m-1", "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    obj, meta = rl.chat_json([{"role": "user", "content": "secret words"}], purpose="t",
                             schema={"type": "object"}, opener=_opener(out, seen))
    assert obj == {"a": 1}
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    log = (priv / rl.STORE_DIRNAME / "calls.jsonl").read_text()
    assert "secret words" not in log and '"ok": true' in log
    assert oct((priv / rl.STORE_DIRNAME).stat().st_mode & 0o777) == "0o700"


def test_chat_refuses_another_model(priv):
    _write_cfg(priv)
    out = {"model": "other", "choices": [{"message": {"content": "x"}}]}
    with pytest.raises(rl.RemoteLLMError, match="answered by other"):
        rl.chat([{"role": "user", "content": "q"}], purpose="t", opener=_opener(out, []))
    assert '"ok": false' in (priv / rl.STORE_DIRNAME / "calls.jsonl").read_text()


def test_store_append_read_purge(priv):
    rl.append("x.jsonl", {"a": 1})
    rl.append("x.jsonl", {"a": 2})
    assert [r["a"] for r in rl.read("x.jsonl")] == [1, 2]
    assert oct((priv / rl.STORE_DIRNAME / "x.jsonl").stat().st_mode & 0o777) == "0o600"
    assert "x.jsonl" in rl.purge()
    assert rl.read("x.jsonl") == []


def test_slots_cap_requests_in_flight(priv, monkeypatch):
    import threading
    import time as _time
    _write_cfg(priv, max_parallel=2)
    state = {"now": 0, "peak": 0}
    guard = threading.Lock()
    out = {"model": "m-1", "choices": [{"message": {"content": "ok"}}]}

    def open_fn(req, timeout=None):
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        _time.sleep(0.15)
        with guard:
            state["now"] -= 1
        return _Resp(json.dumps(out).encode())
    threads = [threading.Thread(target=rl.chat, args=([{"role": "user", "content": "q"}],),
                                kwargs={"purpose": "t", "opener": open_fn}) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 2
    assert rl.max_parallel(rl.config()) == 2


def _hold_every_slot(n):
    import fcntl
    fds = []
    for i in range(n):
        fd = os.open(rl.store_dir() / f"endpoint-{i}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fds.append(fd)
    return fds


def _release(fds):
    import fcntl
    for fd in fds:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_chat_jobs_give_up_when_no_slot_frees(priv, monkeypatch):
    import time as _time
    _write_cfg(priv, max_parallel=2)
    monkeypatch.setattr(rl, "CHAT_SLOT_WAIT_S", 0.3)
    assert rl.CHAT_SLOT_WAIT_S <= 2
    seen = []
    fds = _hold_every_slot(2)
    try:
        t0 = _time.monotonic()
        with pytest.raises(rl.NoSlotError):
            rl.chat([{"role": "user", "content": "q"}], purpose="chat-thread-title",
                    opener=_opener({"model": "m-1", "choices": []}, seen))
        assert _time.monotonic() - t0 < 1.5
    finally:
        _release(fds)
    assert seen == []
    assert '"no free request slot' in (priv / rl.STORE_DIRNAME / "calls.jsonl").read_text()


def test_other_purposes_wait_for_a_slot(priv, monkeypatch):
    import threading
    import time as _time
    _write_cfg(priv, max_parallel=1)
    monkeypatch.setattr(rl, "CHAT_SLOT_WAIT_S", 0.1)
    out = {"model": "m-1", "choices": [{"message": {"content": "ok"}}]}
    got = {}
    fds = _hold_every_slot(1)
    th = threading.Thread(target=lambda: got.update(r=rl.chat(
        [{"role": "user", "content": "q"}], purpose="mail-record", opener=_opener(out, []))))
    th.start()
    _time.sleep(0.5)
    assert th.is_alive()
    _release(fds)
    th.join(5)
    assert got["r"]["content"] == "ok"


def test_n_answers_in_one_request(priv):
    _write_cfg(priv)
    seen = []
    out = {"model": "m-1", "choices": [{"message": {"content": '{"v": %d}' % i}} for i in range(3)]}
    obj, meta = rl.chat_json([{"role": "user", "content": "q"}], purpose="t", schema={},
                             n=3, opener=_opener(out, seen))
    assert seen[0]["n"] == 3
    assert obj == {"v": 0} and [o["v"] for o in meta["objects"]] == [0, 1, 2]


def test_set_writes_one_switch_and_keeps_the_rest(priv, monkeypatch):
    _write_cfg(priv, mail="shadow", drafting="ab")
    r = rl.set_switch("mail", "enforce")
    assert r == {"switch": "mail", "before": "shadow", "after": "enforce", "effective": "enforce"}
    raw = json.loads((priv / rl.CONFIG_FILE).read_text())
    assert raw["mail"] == "enforce" and raw["drafting"] == "ab" and raw["model"] == "m-1"
    assert ((priv / rl.CONFIG_FILE).stat().st_mode & 0o777) == 0o600
    assert rl.switches()["drafting"] == {"set": "ab", "effective": "ab"}
    with pytest.raises(ValueError):
        rl.set_switch("scheduling", "sealed")              # mail only
    with pytest.raises(ValueError):
        rl.set_switch("scheduling", "ghost")               # mail only
    with pytest.raises(ValueError):
        rl.set_switch("drafting", "enforce")
    with pytest.raises(ValueError):
        rl.set_switch("tagger", "remote")
    monkeypatch.setattr(rl, "_owner", lambda: False)
    with pytest.raises(rl.RemoteLLMError):
        rl.set_switch("mail", "off")


def test_switches_show_a_shadow_past_its_end_date_as_off(priv):
    _write_cfg(priv, scheduling="shadow", shadow_until="2000-01-01")
    assert rl.switches()["scheduling"] == {"set": "shadow", "effective": "off"}



def test_a_bad_request_does_not_open_the_breaker_and_the_gateway_has_its_own():
    assert rl._client_fault(rl.RemoteLLMError("HTTP 400: context"))
    assert rl._client_fault(rl.RemoteLLMError("HTTP 413: too large"))
    assert not rl._client_fault(rl.RemoteLLMError("HTTP 429: slow down"))
    assert not rl._client_fault(rl.RemoteLLMError("HTTP 503: down"))
    assert not rl._client_fault(rl.RemoteLLMError("request failed: timeout"))
    names = []

    class B:
        def __init__(self, name, **kw):
            names.append(name)
    import types
    fake = types.SimpleNamespace(CircuitBreaker=B)
    import sys as _sys
    _sys.modules["retry_policy"] = fake
    try:
        rl._breaker("tool-gateway"); rl._breaker("mail-tune-x"); rl._breaker("mail-record")
    finally:
        del _sys.modules["retry_policy"]
    assert names == ["remote-llm-gateway", "remote-llm-tune", "remote-llm"]


def test_key_has_one_generic_name(priv, monkeypatch):
    _write_cfg(priv, drafting="remote")
    monkeypatch.delenv("REMOTE_LLM_API_KEY", raising=False)
    assert rl.api_key() == ""
    assert {k for k in rl._owner_trust.SECRET_KEYS if "LLM" in k} == {"REMOTE_LLM_API_KEY"}
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "the-key")
    assert rl.api_key() == "the-key"
    assert rl.SECRET_NAME == "REMOTE_LLM_API_KEY"
    assert not hasattr(rl._owner_trust, "LEGACY_SECRET_ALIASES")


def test_init_writes_config_with_switches_off(priv, monkeypatch):
    monkeypatch.delenv("REMOTE_LLM_API_KEY", raising=False)
    out = rl.init_config("https://llm.example.edu/v1/", "m-2")
    assert out["created"] and out["endpoint"] == "https://llm.example.edu/v1"
    assert out["switches"] == {k: "off" for k in rl.SWITCHES}
    assert out["key_present"] is False
    cfg = rl.config()
    assert cfg["endpoint"] == "https://llm.example.edu/v1" and cfg["model"] == "m-2"
    assert cfg["drafting"] == "off" and cfg["mail"] == "off"
    # A second init keeps the switches already set and can turn the writer on.
    assert rl.set_switch("digests", "remote")["after"] == "remote"
    out = rl.init_config("https://llm.example.edu/v1", "m-3", drafting="remote")
    assert not out["created"]
    cfg = rl.config()
    assert cfg["model"] == "m-3" and cfg["drafting"] == "remote" and cfg["digests"] == "remote"
    with pytest.raises(ValueError):
        rl.init_config("http://plain.example/v1", "m")
    with pytest.raises(ValueError):
        rl.init_config("https://x.example/v1", "")


def test_init_cli_round_trip(priv, capsys):
    assert rl.main(["init", "--endpoint", "https://llm.example.edu/v1",
                    "--model", "m-1", "--drafting", "remote"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["switches"]["drafting"] == "remote"
    assert rl.main(["init", "--endpoint", "http://nope/v1", "--model", "m"]) == 2
