"""mail_gate and the enforce paths in mail_privacy: what the bulk lanes see
of each class of mail, the held mail, the inline read, the remote digest,
and the cron lanes' reads through the mediator. Nothing here reaches the
network or the real private dir."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402
import mail_gate as mg  # noqa: E402

NOW = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
ME = "owner@example.edu"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg(d, mail="enforce"):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": mail,
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": False}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {ME})
    monkeypatch.setattr(mg, "_list_senders", lambda: {"list@lists.example.org"})
    mp._INDEX_CACHE.update(mtime=None, index=None)
    _cfg(d)
    return d


def _rec(mid, conv, state="fyi", needs=False, kept="", status="ok", frm="alice@example.edu",
         received=NOW - timedelta(hours=1), **res):
    r = {"key": f"k-{mid}", "id": mid, "conversation_id": conv, "received": _iso(received),
         "from": frm, "subject": f"subject {mid}", "status": status, "keep_from_anthropic": kept}
    if status == "ok":
        r["result"] = {"state": state, "needs_reply": needs, "summary": f"summary of {mid}",
                       "ask": res.get("ask", ""), "deadline": res.get("deadline", ""),
                       "offered_times": [], "people": [], "tone": [], "domain": res.get("domain", "research"),
                       "scheduling": "none", "sensitivity": "normal"}
    rl.append(mp.EXTRACTIONS, r)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    return r


def _msg(mid, conv, frm="alice@example.edu", received=NOW - timedelta(hours=1)):
    return {"id": mid, "conversationId": conv, "receivedDateTime": _iso(received),
            "from": {"emailAddress": {"address": frm}}, "subject": f"subject {mid}",
            "bodyPreview": f"RAW TEXT of {mid}"}


def _item(mid, conv):
    return {"message_id": mid, "conversation_id": conv, "subject": f"subject {mid}",
            "body_preview": f"RAW TEXT of {mid}"}


def _gate(msgs, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("graph", object())
    return mg.Gate(msgs, label="test", **kw)


def test_off_passes_everything_through(store):
    _cfg(store, mail="shadow")
    g = _gate([_msg("m1", "c1")])
    items = [_item("m1", "c1")]
    assert g.items(items) == items and g.ready([_msg("m1", "c1")])[0]["id"] == "m1"
    assert g.summary() == {"mode": "off"}


def test_each_class(store):
    _rec("act", "c-act", state="needs-reply", needs=True, ask="send the slides", deadline="2026-10-12")
    _rec("fyi", "c-fyi")
    _rec("kept", "c-kept", state="needs-reply", needs=True, kept="listed domain mil")
    _rec("own", "c-own", frm=ME)
    msgs = [_msg("act", "c-act"), _msg("fyi", "c-fyi"), _msg("kept", "c-kept"), _msg("own", "c-own", frm=ME),
            _msg("new", "c-new"), _msg("old", "c-old", received=NOW - timedelta(hours=9))]
    g = _gate(msgs, budget_s=0.01)
    g.error = ""
    out = g.items([_item(m["id"], m["conversationId"]) for m in msgs])
    by = {o["message_id"]: o for o in out}
    assert set(by) == {"act", "own", "old"}                  # fyi, kept and held left out
    assert "RAW TEXT" not in json.dumps(out)
    assert by["act"]["body_preview"].startswith("summary of act")
    assert "Ask: send the slides" in by["act"]["body_preview"] and "Due: 2026-10-12" in by["act"]["body_preview"]
    assert by["act"]["remote_record"]["needs_reply"] is True
    assert by["old"]["remote_unread"] is True and by["old"]["body_preview"] == ""
    assert "new" in g.held
    assert {m["id"] for m in g.ready(msgs)} == {"act", "fyi", "kept", "own", "old"}   # held mail is not marked seen
    s = g.summary()
    assert s["mode"] == "enforce" and s["held_for_remote_read"] == 1


def test_unread_mail_is_read_inline(store, monkeypatch):
    calls = []

    def fake_process(msg, *, graph, cfg, chat=None):
        calls.append(msg["id"])
        return {"key": f"k-{msg['id']}", "id": msg["id"], "conversation_id": msg["conversationId"],
                "received": msg["receivedDateTime"], "from": "alice@example.edu", "status": "ok",
                "keep_from_anthropic": "",
                "result": {"state": "todo", "needs_reply": False, "summary": "fill the form"}}
    monkeypatch.setattr(mp, "process", fake_process)
    g = _gate([_msg("n1", "c1"), _msg("mine", "c2", frm=ME)])
    assert calls == ["n1"]                                        # the user's own mail is not read
    out = g.items([_item("n1", "c1")])
    assert out[0]["body_preview"] == "summary of n1" or out[0]["body_preview"].startswith("fill the form")


def test_kept_by_address_without_a_record(store):
    g = _gate([_msg("x", "cx", frm="someone@army.mil")], budget_s=0.01)
    assert g.items([_item("x", "cx")]) == [] and "x" not in g.held


def test_strip_keeps_threads_but_not_text_or_kept(store):
    _rec("g1", "c-g1")
    _rec("g2", "c-g2", kept="listed domain mil")
    g = _gate([])
    out = g.strip([{"conversation_id": "c-g1", "latest_preview": "RAW"},
                   {"conversation_id": "c-g2", "latest_preview": "RAW"}])
    assert len(out) == 1 and out[0]["latest_preview"].startswith("summary of g1")


def test_digest_posts_fyi_and_kept_only(store):
    _rec("f1", "c1", domain="teaching", deadline="2026-10-20")
    _rec("k1", "c2", state="needs-reply", needs=True, kept="listed domain mil")
    _rec("a1", "c3", state="needs-reply", needs=True)
    _rec("l1", "c4", frm="list@lists.example.org")
    _rec("o1", "c5", frm=ME)
    posted = []
    r = mg.digest(now=NOW, post=lambda text, now: posted.append(text) or True)
    assert r == {"posted": True, "rows": 2}
    text = posted[0]
    assert "summary of f1" in text and "Due 2026-10-20" in text and "*Teaching*" in text
    assert "Kept from Claude" in text and "Needs your reply" in text
    assert "a1" not in text and "l1" not in text and "o1" not in text
    assert mg.digest(now=NOW + timedelta(minutes=5), post=lambda *a: True) == {"posted": False, "rows": 0}
    _cfg(store, mail="shadow")
    assert "skipped" in mg.digest(now=NOW)


def test_digest_with_no_digest_channel_posts_nothing_and_keeps_its_window(store, monkeypatch,
                                                                          capsys):
    import chat_notify
    _rec("f1", "c1")
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks",
                        lambda: {"ea-assistant": "https://hooks.example/A"})
    assert mg.digest(now=NOW) == {"error": "post failed", "rows": 1}
    assert "no digest channel" in capsys.readouterr().err
    assert rl.read(mg.DIGEST_STATE) in (None, [])
    # on Zulip the post goes to the mail-digest stream, a topic a day
    sent = []
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "zulip")
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: {"zulip": {"site": "s"}})
    monkeypatch.setattr(chat_notify, "_zulip_post_parts",
                        lambda zcfg, target, parts: sent.append(target) or True)
    assert mg.digest(now=NOW) == {"posted": True, "rows": 1}
    assert sent == [{"stream": "mail-digest", "topic": f"Mail digest {NOW.astimezone():%a %b %-d}"}]
    assert rl.read(mg.DIGEST_STATE)[-1]["rows"] == 1


def test_cron_lanes_do_not_read_kept_threads(store):
    _rec("k1", "c-kept", kept="listed domain mil")
    _rec("n1", "c-ok")
    argv = ["get-conversation", "--conversation-id", "c-kept"]
    assert mp.release_precheck("graph_actions", "get-conversation", argv, "cron-morning-briefing-1")
    assert mp.release_precheck("graph_actions", "get-conversation", argv, "run-abc") == ""
    assert mp.release_precheck("graph_actions", "get-message", ["get-message", "--message-id", "k1"],
                               "cron-x") != ""
    assert mp.release_precheck("graph_actions", "get-message", ["get-message", "--message-id", "n1"],
                               "cron-x") == ""
    rows = [{"id": "k1", "conversation_id": "c-kept"}, {"id": "n1", "conversation_id": "c-ok"},
            {"id": "z9", "conversation_id": "c-new", "from": "Pat <someone@navy.mil>"}]
    out, refusal = mp.release_filter("graph_actions", "search-mail", [], "cron-x", json.dumps(rows))
    got = json.loads(out)
    assert refusal == "" and [r.get("id") for r in got[:-1]] == ["n1"] and "_note" in got[-1]
    thread = {"conversation_id": "c-new", "messages": [{"id": "z9", "from": {"address": "someone@navy.mil"}}]}
    out, refusal = mp.release_filter("graph_actions", "get-conversation", [], "cron-x", json.dumps(thread))
    assert out == "" and refusal
    assert mp.release_filter("graph_actions", "search-mail", [], "run-1", json.dumps(rows))[1] == ""
    out, refusal = mp.release_filter("graph_actions", "search-mail", [], "cron-x", "not json")
    assert out == "" and refusal                                  # an unreadable answer is refused
    _cfg(store, mail="shadow")
    assert mp.release_precheck("graph_actions", "get-conversation", argv, "cron-x") == ""


def test_dated_fyi_mail_stays_in_the_digest_until_its_day(store):
    _rec("new", "c1")
    _rec("talk", "c2", deadline="2026-10-11", received=NOW - timedelta(days=3))
    _rec("far", "c3", deadline="2026-10-30", received=NOW - timedelta(days=3))
    _rec("task", "c4", state="todo", deadline="2026-10-11", received=NOW - timedelta(days=3))
    posted = []
    mg.digest(now=NOW, post=lambda text, now: posted.append(text) or True)
    text = posted[0]
    coming = text.split("*Coming up*")[1]
    assert "summary of talk" in coming and "Due 2026-10-11" in coming
    assert "far" not in coming and "task" not in coming
