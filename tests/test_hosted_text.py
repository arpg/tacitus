"""hosted_text: one tool-less hosted call on Claude or on Codex. The
routing table picks the model; a Codex call runs with every tool off,
read-only, as the resident (as the owner for the tagger); a Codex call
with no answer hands the call to Claude once. Each caller (the still-open
check, the reconcile, the backstop, the thread states, the tagger) goes
through it. Nothing here starts a model."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import codex_cli  # noqa: E402
import hosted_text as ht  # noqa: E402


def _answer(text="[]", **over):
    return {"output": text, "tool_calls": [], "harness_error": "", "usage": {"input_tokens": 9}, **over}


def test_the_build_turns_features_off_and_takes_a_named_binary():
    cmd = codex_cli.build(cwd="/w", disable=("shell_tool", "apps"), codex_bin="/opt/codex/bin/codex")
    assert cmd[0] == "/opt/codex/bin/codex" and cmd[-1] == "-"
    assert cmd[cmd.index("--disable") + 1] == "shell_tool" and cmd.count("--disable") == 2
    assert "--disable" not in codex_cli.build(cwd="/w")


def test_a_resident_call_runs_read_only_with_every_tool_off_in_its_own_folder():
    seen = {}

    def run(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return _answer('[{"ref": "R1"}]')
    made = []
    r = ht.codex("judge these", runner=run, scratch_runner=made.append, timeout=90)
    assert r == {"text": '[{"ref": "R1"}]', "cost_usd": None, "rc": 0, "stderr": "",
                 "usage": {"input_tokens": 9}}
    assert seen["sandbox"] == "read-only" and seen["ephemeral"] is True and seen["timeout"] == 90
    assert seen["disable"] == ht.CODEX_OFF and "shell_tool" in ht.CODEX_OFF
    assert seen["cwd"] == ht.text_cwd() and str(seen["cwd"]).endswith(".ea/state/codex-text")
    assert seen["prefix"][:4] == ["sudo", "-n", "-u", ht._resident().RESIDENT]
    assert seen["prompt"].startswith(ht.CODEX_PREAMBLE) and seen["prompt"].endswith("judge these")
    assert "install" in made[0] and str(ht.text_cwd()) in made[0]


def test_a_tool_call_an_error_or_no_answer_is_no_answer():
    for res in (_answer(tool_calls=[{"tool": "Bash"}]), _answer("", harness_error="codex timed out"),
                _answer("")):
        r = ht.codex("p", runner=lambda prompt, **kw: res, scratch_runner=lambda c: None)
        assert r["rc"] == 1 and r["text"] == "" and r["stderr"], res


def test_the_owner_call_runs_as_the_owner_with_its_own_binary_and_no_prefix(monkeypatch):
    seen = {}
    monkeypatch.setattr(ht, "owner_binary", lambda: "/home/o/.nvm/versions/node/v24/bin/codex")
    r = ht.codex("mail text", as_owner=True, runner=lambda prompt, **kw: seen.update(kw) or _answer("{}"))
    assert r["rc"] == 0 and "prefix" not in seen and "last_message_dir" not in seen
    assert seen["codex_bin"].endswith("/bin/codex") and seen["disable"] == ht.CODEX_OFF
    assert seen["env"]["PATH"].startswith("/home/o/.nvm/versions/node/v24/bin")
    monkeypatch.setattr(ht, "owner_binary", lambda: "")
    assert ht.codex("p", as_owner=True, runner=lambda *a, **k: pytest.fail("ran"))["rc"] == 127


def test_run_takes_the_table_s_model_and_hands_a_codex_miss_to_claude_once(monkeypatch):
    calls = []

    def claude(p, m, **kw):
        calls.append(("claude", m, kw))
        return {"text": "claude says", "cost_usd": 0.01, "rc": 0, "stderr": ""}
    monkeypatch.setattr(ht, "model_for", lambda task: "claude")
    r = ht.run("review", "p", "opus", claude=claude, codex_call=lambda *a, **k: pytest.fail("codex"))
    assert r["model_used"] == "claude" and r["fell_back"] == "" and calls == [("claude", "opus", {})]
    monkeypatch.setattr(ht, "model_for", lambda task: "codex")
    r = ht.run("review", "p", "opus", claude=claude,
               codex_call=lambda p, **kw: {"text": "codex says", "rc": 0, "stderr": "", "cost_usd": None})
    assert (r["text"], r["model_used"]) == ("codex says", "codex") and len(calls) == 1
    r = ht.run("thread_state", "p", "opus", timeout=30, claude=claude,
               codex_call=lambda p, **kw: {"text": "", "rc": 1, "stderr": "codex timed out"})
    assert (r["text"], r["model_used"], r["fell_back"]) == ("claude says", "claude", "codex timed out")
    assert calls[-1] == ("claude", "opus", {"timeout": 30})


def test_an_unread_table_runs_on_claude(monkeypatch):
    import model_routing

    def broken(task):
        raise OSError("no table")
    monkeypatch.setattr(model_routing, "hosted_model", broken)
    assert ht.model_for("review") == "claude"


# ── the callers ───────────────────────────────────────────────────────

def test_the_still_open_check_names_codex_and_keeps_kept_mail_out(monkeypatch):
    import open_claims_audit as oca
    import remote_briefing as rb
    import remote_llm
    off = dict(remote_llm._off())
    monkeypatch.setattr(ht, "model_for", lambda task: "codex")
    assert rb.audit_judge(off) == "codex"
    seen = {}
    rec = {"asked": 1, "parked": [], "prompt": "P", "claims": [{"id": "row-1", "description": "d"}]}
    monkeypatch.setattr(rb, "audit_judge", lambda: "codex")
    monkeypatch.setattr(oca, "prepare", lambda **kw: seen.update(kw) or rec)
    monkeypatch.setattr(oca, "apply", lambda audit, verdicts: {"closed": [], "unsure": []})
    out = rb.audit_post("morning-briefing", "post", {}, dry_run=False, judge=lambda p: '{"verdicts": []}',
                        chat=lambda *a, **k: pytest.fail("the remote model was asked"))
    assert out["judge"] == "codex" and seen["claude_safe"] is True

    def down(p):
        raise RuntimeError("codex rc=1")
    out = rb.audit_post("morning-briefing", "post", {}, dry_run=False, judge=down,
                        chat=lambda *a, **k: {"content": '{"verdicts": []}'})
    assert out["judge"] == "remote (Codex failed)"


def test_the_reconcile_and_the_backstop_ask_for_review(monkeypatch):
    import ledger_reconcile as lr
    import sched_agent as sa
    asked = []
    monkeypatch.setattr(ht, "run", lambda task, prompt, model, **kw: asked.append((task, model)) or
                        {"text": '{"verdict": "ok"}', "rc": 0, "cost_usd": None, "model_used": "codex"})
    assert lr._hosted_judge("p") == ('{"verdict": "ok"}', None)
    obj, meta = sa._hosted_json("p", {}, "sonnet")
    assert obj == {"verdict": "ok"} and meta["model_used"] == "codex"
    assert [t for t, _m in asked] == ["review", "review"]
    out = sa.backstop_review({"history": "", "task_block": "", "user_msg": "x"}, {"writes": [], "reply": ""}, [],
                             judge=lambda p, s, m: ({"verdict": "ok"}, {"model_used": "codex"}))
    assert out["model"] == "codex" and out["verdict"] == "ok"
    out = sa.backstop_review({"history": "", "task_block": "", "user_msg": "x"}, {"writes": [], "reply": ""}, [],
                             judge=lambda p, s, m: ({"verdict": "ok"}, {"model_used": "claude"}))
    assert out["model"] == sa.backstop_model()


def test_the_tagger_asks_codex_as_the_owner_and_falls_back_to_claude(monkeypatch):
    import mail_tagger as mt
    monkeypatch.setattr(mt, "_remote_mode", lambda: False)
    monkeypatch.setattr(ht, "model_for", lambda task: "codex" if task == "tagging" else "claude")
    seen = {}
    monkeypatch.setattr(ht, "codex", lambda prompt, **kw: seen.update(kw) or
                        {"text": '{"tags": {"0": ["FYI"]}}', "rc": 0, "stderr": "", "usage": {}})
    verdicts, usage = mt.ask_model("mail text")
    assert verdicts == {"0": ["FYI"]} and usage["model"] == "codex" and seen["as_owner"] is True
    monkeypatch.setattr(ht, "codex", lambda prompt, **kw: {"text": "", "rc": 1, "stderr": "codex timed out"})

    class R:
        returncode, stderr = 0, ""
        stdout = '{"result": "{\\"tags\\": {\\"1\\": [\\"Action\\"]}}", "total_cost_usd": 0.001}'
    ran = []
    monkeypatch.setattr(mt.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or R())
    verdicts, usage = mt.ask_model("mail text", "haiku")
    assert verdicts == {"1": ["Action"]} and usage["model"] == "haiku" and "-p" in ran[0]
