"""An exact owner-reviewed capability precedes any generic form disclosure."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import browser_gate as G
import web_browser as W


def item():
    return {"verb": "submit", "url": "https://forms.example/request", "origins": ["https://forms.example"],
            "actions": [{"action": "fill", "label": "Name", "value": "Test Person"}],
            "submit_selector": "button[type=submit]", "success_text": "Recorded"}


def test_exact_url_answers_origins_and_files_are_bound_to_one_use(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    original = G.C.consume_token
    monkeypatch.setattr(G.C, "mediated_install", lambda: True)
    monkeypatch.setattr(G.C, "consume_token", lambda raw, plugin, card_id, plan:
                        original(raw, plugin, card_id, plan, path=path))
    approved = item()
    approved["actions"].append({"action": "upload", "selector": "input[type=file]",
                                "path": "/tmp/proposal.pdf", "sha256": "a" * 64})
    token = G.C.mint_token("core", G.CARD_ID, approved, path=path)
    monkeypatch.setenv(G.TOKEN_ENV, token)
    for key, value in [("url", "https://forms.example/other"), ("verb", "fill"),
                       ("origins", ["https://forms.example", "https://evil.example"])]:
        changed = copy.deepcopy(approved)
        changed[key] = value
        with pytest.raises(ValueError):
            G.require(changed)
    for field, value in [("value", "Other Person"), ("label", "Other field")]:
        changed = copy.deepcopy(approved)
        changed["actions"][0][field] = value
        with pytest.raises(ValueError):
            G.require(changed)
    changed = copy.deepcopy(approved)
    changed["actions"][1]["sha256"] = "b" * 64
    with pytest.raises(ValueError):
        G.require(changed)
    G.require(approved)
    with pytest.raises(ValueError):
        G.require(approved)


def test_data_entry_blocks_unapproved_origins_and_https_downgrade(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(W.F, "validate_url", lambda url: pytest.fail("must refuse before DNS"))
    route = SimpleNamespace(abort=lambda: calls.append("abort"))
    policy = {"entered_data": True, "origins": ["https://forms.example"]}
    for url in ["https://evil.example/upload", "http://forms.example/save", "https://forms.example:444/save"]:
        blocked = []
        W.route_handler(blocked, policy)(route, SimpleNamespace(url=url))
        assert blocked
    assert calls == ["abort"] * 3


def test_form_writes_fail_closed_without_mediation(monkeypatch):
    monkeypatch.setattr(G.C, "mediated_install", lambda: False)
    monkeypatch.setenv(G.TOKEN_ENV, "resident-created-token")
    with pytest.raises(ValueError, match="mediated confirmation"):
        G.require(item())


def test_owner_executor_drops_identity_and_environment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(G.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setenv("OWNER_SECRET", "not-forwarded")
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(stdout='{"ok":true,"submitted":true}')
    result = G.execute(item(), "owner-capability", tree=tmp_path, ea_dir=str(tmp_path / "ea"), runner=run)
    argv, kw = calls[0]
    assert argv[:6] == ["sudo", "-n", "-u", "ea-bot", "env", "-i"]
    assert kw["env"] == {"PATH": "/usr/bin:/bin"}
    assert f"{G.TOKEN_ENV}=owner-capability" in argv
    assert "not-forwarded" not in str(calls) and result["submitted"] is True


def test_card_cannot_approve_hidden_or_truncated_payload():
    plan = item()
    plan["actions"][0]["value"] = "secret" * 1500
    with pytest.raises(ValueError, match="too large"):
        G.render(plan)
    plan = item()
    plan["actions"][0]["value"] = "invoice\u202e.pdf"
    with pytest.raises(ValueError, match="hidden"):
        G.render(plan)
    plan = item()
    plan["actions"][0]["value"] = "<https://evil.example|safe.example>"
    review = G.render(plan)
    assert G.C._ct.INERT_OPEN in review["lines"][3]


def test_slack_shows_every_approved_answer_without_markdown_or_truncation():
    from chat.cards import ConfirmationCard, CardAction
    from chat.slack_frontend import render_card
    plan = item()
    plan["actions"] = [{"action": "fill", "label": "First", "value": "<" * 1400 + "FIRST-END"},
                       {"action": "fill", "label": "Second", "value": "&" * 1400 + "SECOND-END"}]
    review = G.render(plan)
    card = ConfirmationCard(kind="browser", title=review["title"], lines=review["lines"],
                            actions=[CardAction("approve_browser", "Approve", "ref")])
    _, blocks = render_card(card)
    texts = [b["text"] for b in blocks if b["type"] == "section"]
    assert all(t["type"] == "plain_text" and len(t["text"]) <= 2800 for t in texts)
    shown = "".join(t["text"] for t in texts)
    assert "FIRST-END" in shown and "SECOND-END" in shown
    assert "<" * 1400 in shown and "&" * 1400 in shown


def test_bot_registers_owner_check_claim_and_sweeper():
    import ast
    source = (Path(__file__).resolve().parents[1] / "ea-chat-bot.py").read_text()
    tree = ast.parse(source)
    functions = {n.name: ast.get_source_segment(source, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    handler = functions["handle_approve_browser"]
    for check in ["_verify_button_user", "_reject_if_stale_confirmation", "_claim_plugin_card"]:
        assert check in handler
    assert handler.index("_verify_button_user") < handler.index("threading.Thread")
    assert '("browser", "pending-browser.json")' in source
    assert "BROWSER_ACTION_FILES" in functions["_pending_events_poller"]


def test_browser_saturation_never_blocks_the_mail_queue(tmp_path, monkeypatch):
    import mail_mediator as M
    from types import SimpleNamespace
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setattr(M.M, "QUEUE", queue)
    monkeypatch.setattr(M, "AUDIT", tmp_path / "audit")
    monkeypatch.setattr(M, "_browser_slots", SimpleNamespace(acquire=lambda blocking: False))
    monkeypatch.setattr(M, "_execute", lambda *a, **kw: pytest.fail("must not run on the queue thread"))
    request = queue / "req-busy.json"
    request.write_text(json.dumps({"tool": "web_browser", "verb": "read",
                                   "argv": ["read", "--url", "https://forms.example"]}))
    M.handle(str(request))
    result = json.loads((queue / "resp-busy.json").read_text())
    assert result["rc"] == 3 and "no action started" in result["stderr"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
