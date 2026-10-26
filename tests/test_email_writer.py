"""email_writer: the switch, the body written to --out and recorded, the
fallbacks (NEED, empty, endpoint error, late, unreadable draft), the
thread and current-draft sections, the brief cap and the refused brief
paths. Nothing here reaches the network or the real private dir."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import draft_ab as ab  # noqa: E402
import email_writer as ew  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import resident_io  # noqa: E402

BRIEF = ("Recipient: pat@example.edu. Pat asks whether the slides are ready. "
         "Reply: yes, they go out tonight.")
DRAFT_HTML = ("<html><body><p>Hi Pat,</p><p>Yes, tomorrow.</p><p>Kit</p><hr>"
              "<div id=\"divRplyFwdMsg\">From: Pat<br>Are the slides ready?</div></body></html>")


def _cfg(drafting="remote"):
    return {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off",
            "scheduling": "off", "shadow_until": "2000-01-01", "drafting": drafting}


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setenv("EA_RUN_ID", "run-7")
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(ab, "_style_profile", lambda addr: json.dumps({"found": True, "contact": addr}))
    monkeypatch.setattr(resident_io, "read_text", lambda p: None)
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(_cfg()).encode(), mode=0o600)
    return d


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir(mode=0o700)
    return w


def _brief(work, text=BRIEF):
    p = work / "brief.txt"
    p.write_text(text)
    os.chmod(p, 0o600)
    return p


def _chat(text, monkeypatch=None, delay=0.0):
    def fn(messages, **kw):
        fn.messages, fn.kw = messages, kw
        if delay:
            time.sleep(delay)
        if isinstance(text, Exception):
            raise text
        return {"content": text, "contents": [text], "latency_ms": 800}
    if monkeypatch is not None:
        monkeypatch.setattr(rl, "chat", fn)
    return fn


def _msg(name, addr, when, text, to=("kit@example.edu",)):
    return {"from": {"emailAddress": {"name": name, "address": addr}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "receivedDateTime": when,
            "body": {"contentType": "text", "content": text}}


class Graph:
    def __init__(self, thread=(), draft_html=DRAFT_HTML, fail=()):
        self.thread, self.draft_html, self.fail = list(thread), draft_html, set(fail)

    def get_conversation_messages(self, conversation_id, top=50, include_drafts=False):
        if "thread" in self.fail:
            raise RuntimeError("graph 503")
        return list(self.thread), False

    def get_message_full(self, mid):
        if "draft" in self.fail:
            raise RuntimeError("graph 404")
        return {"body": {"contentType": "HTML", "content": self.draft_html},
                "conversationId": "conv-from-draft"}


def _run(capsys, *argv):
    code = ew.main(["write", *map(str, argv)])
    cap = capsys.readouterr()
    return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def test_switch_off_falls_back(store, work, capsys, monkeypatch):
    chat = _chat("Hi Pat,\n\nYes.\n\nKit", monkeypatch)
    out = work / "body.txt"
    for drafting in ("ab", "off"):
        owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(_cfg(drafting)).encode(),
                                     mode=0o600)
        code, ans, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work),
                            "--out", out)
        assert code == 4 and ans == {"status": "fallback", "reason": "writer off"}
    monkeypatch.setattr(rl, "_owner", lambda: False)          # the resident never writes
    code, ans, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work),
                        "--out", out)
    assert code == 4 and ans["reason"] == "writer off"
    assert not out.exists() and not hasattr(chat, "messages")


def test_ok_writes_the_body_to_out_and_records_it(store, work, capsys, monkeypatch):
    chat = _chat("Hi Pat,\n\nYes — tonight.\n\nKit\n\n---\nNote: attach the PDF.", monkeypatch)
    out = work / "body.txt"
    code, ans, err = _run(capsys, "--to", "Pat@Example.edu", "--brief-file", _brief(work),
                          "--out", out)
    assert code == 0, err
    body = out.read_text()
    assert body.startswith("Hi Pat,") and body.endswith("Kit")
    assert "—" not in body and "attach the PDF" not in body
    assert ans == {"status": "ok", "writer": "remote", "out": str(out), "words": len(body.split()),
                   "notes": "---\nNote: attach the PDF.", "body": body}
    assert chat.kw["purpose"] == "draft-writer" and 5 <= chat.kw["timeout"] <= 90
    system, user = chat.messages[0]["content"], chat.messages[1]["content"]
    assert '"contact": "pat@example.edu"' in system and user.startswith(BRIEF)
    assert "=== The thread" not in user and "=== The current draft" not in user
    rec = rl.read(ew.STORE)[-1]
    assert rec["status"] == "ok" and rec["run_id"] == "run-7"
    assert rec["recipient_domain"] == "example.edu" and rec["body"] == body
    assert rec["notes"] == ans["notes"] and rec["latency_ms"] == 800
    assert rec["conversation_id"] == "" and rec["draft_id"] == ""
    got = ew.recent_bodies("2000-01-01T00:00:00Z")
    assert [r["body"] for r in got] == [body] and got[0]["run_id"] == "run-7"
    assert ew.recent_bodies("2999-01-01T00:00:00+00:00") == []


def test_need_falls_back_with_the_question(store, work, capsys, monkeypatch):
    _chat("NEED: the date of the talk", monkeypatch)
    out = work / "body.txt"
    code, ans, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work),
                        "--out", out)
    assert code == 4 and ans["status"] == "fallback" and ans["need"] == "the date of the talk"
    assert not out.exists()
    rec = rl.read(ew.STORE)[-1]
    assert rec["status"] == "need" and rec["body"] == "" and rec["need"] == "the date of the talk"
    assert ew.recent_bodies("2000-01-01T00:00:00Z") == []


@pytest.mark.parametrize("answer, status", [
    ("", "empty"),
    (rl.RemoteLLMError("request failed: timed out"), "timeout"),
    (rl.RemoteLLMError("HTTP 502: bad gateway"), "error"),
])
def test_unusable_answers_fall_back(store, work, answer, status):
    out = work / "body.txt"
    ans, code = ew.write("pat@example.edu", BRIEF, str(out), chat=_chat(answer))
    assert code == 4 and ans["status"] == "fallback" and "need" not in ans
    assert not out.exists()
    assert rl.read(ew.STORE)[-1]["status"] == status


def test_late_answer_falls_back_within_the_budget(store, work):
    out = work / "body.txt"
    t0 = time.monotonic()
    ans, code = ew.write("pat@example.edu", BRIEF, str(out), budget=1,
                         chat=_chat("Hi Pat,\n\nYes.\n\nKit", delay=5))
    assert time.monotonic() - t0 < 4
    assert code == 4 and ans["reason"].startswith("late")
    assert rl.read(ew.STORE)[-1]["status"] == "timeout" and not out.exists()


def test_thread_and_current_draft_sections(store, work, capsys, monkeypatch):
    thread = [
        _msg("Kit Calloway", "kit@example.edu", "2026-10-07T09:00:00Z",
             "Slides by Friday?", to=("pat@example.edu",)),
        _msg("Pat Lee", "pat@example.edu", "2026-10-08T14:03:00Z",
             "Are the slides ready?\n\nFrom: Kit Calloway\nSlides by Friday?"),
    ]
    monkeypatch.setitem(sys.modules, "graph_client", Graph(thread=thread))
    chat = _chat("Hi Pat,\n\nYes, tonight.\n\nKit", monkeypatch)
    code, ans, err = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work),
                          "--out", work / "body.txt", "--conversation-id", "conv-1",
                          "--draft-id", "draft-9")
    assert code == 0, err
    user = chat.messages[1]["content"]
    assert user.startswith(BRIEF)
    t = user.index("=== The thread (oldest first) ===")
    d = user.index("=== The current draft. Revise it as the request says; keep what the "
                   "request does not change. ===")
    assert t < d
    section = user[t:d]
    assert section.index("Slides by Friday?") < section.index("Are the slides ready?")
    assert "From: Pat Lee <pat@example.edu>" in section and section.count("Slides by Friday?") == 1
    assert user[d:].endswith("Hi Pat,\nYes, tomorrow.\nKit")      # the quoted thread cut
    rec = rl.read(ew.STORE)[-1]
    assert rec["conversation_id"] == "conv-1" and rec["draft_id"] == "draft-9"
    assert rec["thread_messages"] == 2


def test_thread_keeps_the_last_messages_within_the_cap(store):
    thread = [_msg("Pat Lee", "pat@example.edu", f"2026-10-0{i}T10:00:00Z", f"M{i} " + "x" * 3800)
              for i in range(1, 9)]
    text, shown = ew.thread_text("conv-1", graph=Graph(thread=thread))
    assert shown == 3 and len(text) <= ew.THREAD_CHARS
    assert text.index("M6 ") < text.index("M7 ") < text.index("M8 ") and "M5 " not in text
    big = [_msg("Pat Lee", "pat@example.edu", "2026-10-08T10:00:00Z", "y" * 30000)]
    text, shown = ew.thread_text("conv-1", graph=Graph(thread=big))
    assert shown == 1 and text.count("y") == ew.MESSAGE_CHARS


def test_draft_id_takes_the_drafts_conversation_and_a_dead_draft_falls_back(store, work):
    chat = _chat("Hi Pat,\n\nYes.\n\nKit")
    ans, code = ew.write("pat@example.edu", BRIEF, str(work / "a.txt"), draft_id="d1",
                         graph=Graph(), chat=chat)
    assert code == 0 and rl.read(ew.STORE)[-1]["conversation_id"] == "conv-from-draft"
    ans, code = ew.write("pat@example.edu", BRIEF, str(work / "b.txt"), draft_id="d2",
                         graph=Graph(fail={"draft"}), chat=chat)
    assert code == 4 and ans["reason"].startswith("draft unreadable")
    assert not (work / "b.txt").exists()


def test_an_unreadable_thread_still_writes(store, work):
    ans, code = ew.write("pat@example.edu", BRIEF, str(work / "a.txt"), conversation_id="c1",
                         graph=Graph(fail={"thread"}), chat=_chat("Hi Pat,\n\nYes.\n\nKit"))
    assert code == 0 and rl.read(ew.STORE)[-1]["thread_error"] == "graph 503"


def test_the_brief_is_capped(store, work, capsys, monkeypatch):
    chat = _chat("Hi Pat,\n\nYes.\n\nKit", monkeypatch)
    brief = _brief(work, "A" * ew.BRIEF_CHARS + "B" * 10_000)
    code, _, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", brief,
                      "--out", work / "body.txt")
    assert code == 0
    user = chat.messages[1]["content"]
    assert user.startswith("A" * ew.BRIEF_CHARS) and "B" not in user
    assert len(rl.read(ew.STORE)[-1]["brief"]) == 20_000


def test_bad_arguments_are_refused(store, work, tmp_path, capsys, monkeypatch):
    chat = _chat("Hi Pat,\n\nYes.\n\nKit", monkeypatch)
    out = work / "body.txt"
    code, ans, err = _run(capsys, "--to", "not-an-address", "--brief-file", _brief(work),
                          "--out", out)
    assert code == 2 and ans is None and json.loads(err)["status"] == "error"
    link = work / "link.txt"
    link.symlink_to(_brief(work))
    code, _, err = _run(capsys, "--to", "pat@example.edu", "--brief-file", link, "--out", out)
    assert code == 2 and "symlink" in json.loads(err)["message"]     # never followed
    code, _, err = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work, "  \n"),
                        "--out", out)
    assert code == 2 and "empty" in json.loads(err)["message"]
    assert not out.exists() and not hasattr(chat, "messages") and rl.read(ew.STORE) == []


def test_a_resident_run_falls_back_before_it_reads_the_brief(store, tmp_path, capsys, monkeypatch):
    # An install without mail mediation: ea-bot runs the CLI itself and
    # writes its brief in its own state directory, which is group-writable.
    chat = _chat("Hi Pat,\n\nYes.\n\nKit", monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    os.chmod(state, 0o2775)
    brief = state / "pat-brief.txt"
    brief.write_text(BRIEF)
    out = state / "reply-body.txt"
    monkeypatch.setattr(ew, "read_brief", lambda path: pytest.fail("brief read"))
    monkeypatch.setattr(rl, "_owner", lambda: False)
    code, ans, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", brief, "--out", out)
    assert code == 4 and ans == {"status": "fallback", "reason": "writer off"}
    assert not out.exists() and not hasattr(chat, "messages")
    code, _, err = _run(capsys, "--to", "not-an-address", "--brief-file", brief, "--out", out)
    assert code == 2 and json.loads(err)["status"] == "error"


def test_finish_body_kinds():
    assert ab.finish_body("NEED: the date")[1]["kind"] == "need"
    assert ab.finish_body("  ")[1]["kind"] == "empty"
    body, meta = ab.finish_body("Hi Pat,\n\nYes — tonight.\n\nKit")
    assert body == "Hi Pat,\n\nYes, tonight.\n\nKit" and "kind" not in meta


def test_each_brief_sent_is_recorded_owner_side(store, work, capsys, monkeypatch):
    import outbound_log
    monkeypatch.setattr(outbound_log._owner_trust, "is_owner_process", lambda: True)
    chat = _chat("Hi Pat,\n\nYes.\n\nKit", monkeypatch)
    brief = BRIEF + " " + "x" * 300
    code, _, _ = _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work, brief),
                      "--out", work / "body.txt", "--conversation-id", "")
    assert code == 0
    log = store / "logs" / outbound_log.OWNER_LOG_NAME
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["event"] == "remote_brief" and rec["source"] == "email_writer"
    assert rec["host"] == "llm.example.edu" and rec["run_id"] == "run-7"
    assert rec["brief_chars"] == len(brief) and rec["brief_head"] == brief[:200]
    assert rec["brief_sha256"] == __import__("hashlib").sha256(brief.encode()).hexdigest()
    sent = sum(len(m["content"]) for m in chat.messages)
    assert rec["sent_chars"] == sent and sent > len(brief)
    assert "body" not in rec and (log.stat().st_mode & 0o777) == 0o600
    # the writer off sends nothing and records nothing
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(_cfg("off")).encode(),
                                 mode=0o600)
    _run(capsys, "--to", "pat@example.edu", "--brief-file", _brief(work), "--out", work / "b2.txt")
    assert len(log.read_text().splitlines()) == 1
