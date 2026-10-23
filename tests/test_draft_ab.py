"""draft_ab: which runs take part, the remote model's context, the arm
draw and the body swap, the fallback to Claude's body, the edit measure
at send, and the scorecard. Nothing here reaches the network or the real
private dir."""
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import draft_ab as ab  # noqa: E402

PROMPT = ("Recipient: morgan.vasquez41@example.edu. Thread: Pat asks whether the slides are ready. "
          "Reply: yes, attached tonight. Details in /home/ea-bot/.ea/state/ctx-1.txt")
CLAUDE_HTML = ("<html><body><p>Hi Pat,</p><p>Yes, I will send the slides tonight.</p>"
               "<p>Kit</p><hr><div id=\"divRplyFwdMsg\">From: Pat<br>Are the slides ready?</div>"
               "</body></html>")


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(ab, "_style_profile", lambda addr: json.dumps({"found": True, "contact": addr}))
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off",
           "scheduling": "off", "shadow_until": "2099-01-01", "drafting": "ab", "qwen_share": 0.5}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


class Graph:
    def __init__(self, html=CLAUDE_HTML):
        self.html, self.updates = html, []

    def get_message_full(self, mid):
        return {"body": {"contentType": "HTML", "content": self.html}, "conversationId": "conv1",
                "toRecipients": [{"emailAddress": {"address": "morgan.vasquez41@example.edu"}}]}

    def update_draft(self, mid, body):
        self.updates.append((mid, body))
        self.html = "<html><body>" + "".join(f"<p>{ln}</p>" for ln in body.splitlines()) + "</body></html>"


class Rng:
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


HANDBACK = ("[Subagent hand-back] The text below is the final report of a subagent this "
            "session spawned.\n\nHi Pat,\nYes, I will send the slides tonight.\nKit")


def _calls(prompt=PROMPT, result=HANDBACK):
    return [("Bash", 1.0, "", "", {"command": "ls"}),
            ("Agent", 30.0, "", result, {"subagent_type": "email-author", "prompt": prompt})]


def _chat(text):
    def fn(messages, **kw):
        fn.messages = messages
        return {"content": text, "contents": [text], "latency_ms": 900}
    return fn


def test_only_email_author_calls_take_part():
    assert ab.author_calls(_calls()) == [{"prompt": PROMPT, "result": HANDBACK}]
    assert ab.author_calls([("Agent", 1.0, "", "", {"subagent_type": "general-purpose", "prompt": "x"})]) == []


def test_author_match_pairs_a_draft_with_the_text_it_holds():
    body = "Hi Pat,\nYes, I will send the slides tonight.\nKit"
    assert ab.author_match(HANDBACK, body) == 1.0                    # preamble, then the body
    cut = ab.author_match("[Subagent hand-back] ...\n\nHi Pat,\nYes, I will send the", body)
    assert ab.AUTHOR_MATCH <= cut < 1.0                               # cut short, still its text
    other = ("Dear Dr. Lee,\nI heard your talk at the PI meeting last month. I advise a startup "
             "building robots and would like to know when it would suit you for me to introduce "
             "the team.\nBest,\nKit")
    assert ab.author_match(other, body) < ab.AUTHOR_MATCH             # another email's text
    assert ab.author_match("", body) == 0.0 and ab.author_match(HANDBACK, "") == 0.0


def test_a_call_for_another_email_does_not_claim_the_writers_draft(store, monkeypatch):
    """One run: the writer made the draft, and the subagent wrote a
    different email (its contact has no address yet). The draft keeps its
    body in both modes; the remote mode records it as the writer's."""
    import email_writer
    other_call = ("Agent", 30.0, "",
                  "Dear Dr. Lee,\nI heard your talk at the PI meeting last month. I advise a "
                  "startup building robots and would like to introduce the team.\nBest,\nKit",
                  {"subagent_type": "email-author",
                   "prompt": "Write a first email to Dr. Lee; the address is not known yet."})
    monkeypatch.setattr(email_writer, "recent_bodies", lambda since: [
        {"run_id": "run-9", "body": "Hi Pat,\nYes, I will send the slides tonight.\nKit",
         "recipient_domain": "example.edu"}])
    g = Graph()
    chat = _chat("Dear Dr. Lee,\n\nI enjoyed your talk.\n\nBest,\nKit")
    assert ab.assign("m1", [other_call], run_id="run-9", graph=g, chat=chat, rng=Rng(0.1),
                     background=False) == {}
    assert g.updates == [] and ab.assignment("m1") is None
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "drafting": "remote"}).encode(),
        mode=0o600)
    rec = ab.assign("m2", [other_call], run_id="run-9", graph=g, chat=chat, background=False)
    assert rec["mode"] == "writer" and rec["qwen_body"].startswith("Hi Pat") and g.updates == []


def test_own_text_cuts_the_quoted_thread():
    msg = Graph().get_message_full("d")
    assert ab.own_text(msg) == "Hi Pat,\nYes, I will send the slides tonight.\nKit"
    plain = {"body": {"contentType": "text", "content": "Thanks!\nKit\n\nFrom: Pat\nold text"}}
    assert ab.own_text(plain) == "Thanks!\nKit"


def test_edit_ratio():
    assert ab.edit_ratio("a b c d", "a b c d") == 0.0
    assert 0 < ab.edit_ratio("a b c d", "a b x d") < 0.5
    assert ab.edit_ratio("a b", "c d") == 1.0


def test_remote_gets_the_subagent_context(store, monkeypatch):
    import resident_io
    monkeypatch.setattr(resident_io, "read_text", lambda p: "CONTEXT FILE TEXT" if "ctx-1" in str(p) else None)
    msgs = ab.remote_messages(PROMPT, "morgan.vasquez41@example.edu")
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "You write email and letter bodies" in system        # the agent's instructions
    assert '"contact": "morgan.vasquez41@example.edu"' in system             # the style profile
    assert "drafting-voice.md" in system and "Default to short" not in user  # the voice guide
    assert user.startswith(PROMPT) and "CONTEXT FILE TEXT" in user


def test_remote_arm_swaps_the_body(store):
    g = Graph()
    rec = ab.assign("d1", _calls(), graph=g, chat=_chat("Hi Pat,\n\nYes — tonight.\n\nKit"),
                    rng=Rng(0.1), background=False)
    assert rec["arm"] == "qwen" and rec["shown"] == "qwen"
    assert g.updates and "—" not in g.updates[0][1]          # the dash fix ran
    assert rec["claude_body"].startswith("Hi Pat,")               # Claude's version kept
    assert ab.assignment("d1")["qwen_body"] == g.updates[0][1]


def test_remote_failure_keeps_claudes_body(store):
    g = Graph()
    rec = ab.assign("d2", _calls(), graph=g, chat=_chat("NEED: the date"), rng=Rng(0.1),
                    background=False)
    assert rec["arm"] == "qwen" and rec["shown"] == "claude" and rec["fallback"].startswith("asked")
    assert g.updates == []


def test_claude_arm_and_switch_off(store):
    g = Graph()
    assert ab.assign("d3", _calls(), graph=g, chat=_chat("x"), rng=Rng(0.9),
                     background=False)["shown"] == "claude"
    assert g.updates == []
    assert ab.assign("d4", [], graph=g) == {}
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "drafting": "off",
         "shadow_until": "2099-01-01"}).encode(), mode=0o600)
    assert ab.assign("d5", _calls(), graph=g) == {}


def test_send_records_edits_and_the_other_version(store):
    g = Graph()
    ab.assign("d6", _calls(), graph=g, chat=_chat("Hi Pat,\n\nYes, tonight.\n\nKit"),
              rng=Rng(0.1), background=False)
    assert ab.snapshot("nobody", graph=g) is None                 # outside the experiment
    g.html = "<html><body><p>Hi Pat,</p><p>Yes, tonight. Thanks!</p><p>Kit</p></body></html>"
    ab.note_send("d6", ab.snapshot("d6", graph=g))
    out = rl.read(ab.OUTCOMES)[-1]
    assert out["outcome"] == "sent" and out["shown"] == "qwen"
    assert 0 < out["edit_ratio"] < 0.4 and out["unchanged"] is False
    assert "other_edit_ratio" in out


def test_scorecard(store):
    for i, (v, sent_html) in enumerate([(0.1, None), (0.9, None), (0.1, "cancel")]):
        g = Graph()
        ab.assign(f"s{i}", _calls(), graph=g, chat=_chat("Hi Pat,\n\nYes, tonight.\n\nKit"),
                  rng=Rng(v), background=False)
        if sent_html == "cancel":
            ab.note_cancel(f"s{i}")
        else:
            ab.note_send(f"s{i}", ab.snapshot(f"s{i}", graph=g))
    r = ab.report()
    assert r["drafts"] == 3
    assert r["qwen"]["drafts"] == 2 and r["qwen"]["sent"] == 1 and r["qwen"]["cancelled"] == 1
    assert r["qwen"]["unchanged"] == 1 and r["claude"]["unchanged"] == 1
    assert r["edit_ratio_diff_ci"] == (None, None)                # too few to say


def test_the_profile_is_read_for_the_contact_the_prompt_names(monkeypatch):
    import mail_tagger
    monkeypatch.setattr(mail_tagger, "_user_addresses", lambda: {"emerson.vasquez27@example.edu"})
    msg = {"toRecipients": [{"emailAddress": {"address": "emerson.thornbury24@example.edu"}}]}
    assert ab._recipient(msg, "From emerson.vasquez27@example.edu to finley.kestrel1@example.edu: say yes") == "finley.kestrel1@example.edu"
    assert ab._recipient(msg, "Say yes.") == "emerson.thornbury24@example.edu"


def test_notes_to_the_caller_are_split_off():
    text = ("Body text for the reply to Tom. Note: attach the two-pager.\n\nHi Tom,\n\n"
            "Here it is.\n\nKit\n\n---\nNote: attach the PDF.")
    body, notes = ab.split_notes(text)
    assert body == "Hi Tom,\n\nHere it is.\n\nKit"
    assert "two-pager" in notes and "attach the PDF" in notes
    assert ab.split_notes("Hi Pat,\nThanks.\nKit") == ("Hi Pat,\nThanks.\nKit", "")


def test_remote_mode_writes_every_draft_with_no_draw_and_no_end_date(store):
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "drafting": "remote",
         "shadow_until": "2000-01-01"}).encode(), mode=0o600)
    assert rl.config()["drafting"] == "remote" and ab.mode() == "remote"
    g = Graph()
    rec = ab.assign("r1", _calls(), graph=g, chat=_chat("Hi Pat,\n\nYes, tonight.\n\nKit"),
                    rng=Rng(0.99))
    assert rec["mode"] == "remote" and rec["shown"] == "qwen" and g.updates
    g2 = Graph()
    rec = ab.assign("r2", _calls(), graph=g2, chat=_chat("NEED: the date"), rng=Rng(0.99))
    assert rec["shown"] == "claude" and rec["fallback"] and g2.updates == []
    assert not [r for r in rl.read(ab.ASSIGN) if "shadow_qwen_body" in r]   # no other version
    ab.note_send("r1", ab.snapshot("r1", graph=g))
    r = ab.report()
    assert r["mode"] == "remote" and r["drafts"] == 0                   # not part of the A/B
    w = r["remote_writer"]
    assert w["drafts"] == 2 and w["fell_back_to_claude"] == 1 and w["sent"] == 1 and w["unchanged"] == 1


def test_a_writer_draft_is_matched_and_measured(store, monkeypatch):
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "drafting": "remote"}).encode(),
        mode=0o600)
    import email_writer
    monkeypatch.setattr(email_writer, "recent_bodies", lambda since: [
        {"run_id": "run-1", "body": "Hi Pat,\nYes, I will send the slides tonight.\nKit",
         "recipient_domain": "example.edu"},
        {"run_id": "run-2", "body": "Something else entirely", "recipient_domain": "x.org"}])
    g = Graph()
    rec = ab.assign("w1", [("Bash", 1.0, "", "", {"command": "ls"})], run_id="run-1", graph=g)
    assert rec["mode"] == "writer" and rec["shown"] == "qwen" and g.updates == []
    assert ab.assign("w2", [("Bash", 1.0, "", "", {"command": "ls"})], run_id="run-2", graph=g) == {}
    ab.note_send("w1", ab.snapshot("w1", graph=g))
    assert ab.report()["remote_writer"]["sent"] == 1


def test_the_writer_is_told_who_signs(store, monkeypatch):
    import resident_io
    profile = json.dumps({"name": "Kit Calloway", "email": "wren.fairbanks84@example.edu"})
    monkeypatch.setattr(resident_io, "read_text",
                        lambda p: profile if str(p).endswith("user-profile.json") else None)
    system = ab.remote_messages("Reply to Pat: yes.", "morgan.vasquez41@example.edu")[0]["content"]
    assert 'You write as Kit Calloway. He signs with "Kit" alone' in system
    assert "Never take his name from an email address" in system
    monkeypatch.setattr(resident_io, "read_text", lambda p: None)
    assert "THE SENDER" not in ab.remote_messages("Reply.", "morgan.vasquez41@example.edu")[0]["content"]
