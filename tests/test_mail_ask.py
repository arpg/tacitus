"""Questions about mail (mail_ask.py, `mail_actions.py ask`): the local model
reads a thread's whole text, a forward's forwarded part included; the keep
levels hold (facts refused, below full in the model's own words and no
files, full may quote and reads the PDFs); the mediator runs the verb as a
read on its own thread, and a cron lane's question about a kept thread is
refused before it runs. Made-up people, domains and ids; every Graph and
model call is a stub."""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import codex_ghost as cg  # noqa: E402
import mail_actions  # noqa: E402
import mail_ask  # noqa: E402
import mail_mediation as MM  # noqa: E402
import mail_mediator  # noqa: E402
import mail_privacy as mp  # noqa: E402
import owner_trust  # noqa: E402
import preferences as prefs  # noqa: E402
import privacy_rules as pr  # noqa: E402
import remote_llm as rl  # noqa: E402

ME = "owner@example.edu"
ORGANIZER = "sam.organizer@consortium.example.org"
OFFICER = "lee.officer@unit.example.mil"
CONV = "AAQkAGI2TG93AAA" + "q" * 30 + "="
MID1 = "AAMkAGI2TG93AAA" + "a" * 40 + "="
MID2 = "AAMkAGI2TG93AAA" + "b" * 40 + "="
MID3 = "AAMkAGI2TG93AAA" + "d" * 40 + "="
CFG = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "ghost",
       "scheduling": "off", "shadow_until": "2099-01-01",
       "keep_from_frontier": {"domains": ["mil"], "addresses": [], "admissions": True}}

GAP = ("Operators get conflicting reports from three sensor feeds and need one picture they can "
       "trust, with an alert when the system's confidence drops below a set level.")
FORWARD = (f"Here is Lee's gap and bio.\n\nSam\n\nFrom: Lee Officer <{OFFICER}>\n"
           f"Sent: Monday, November 9, 2026 9:00 AM\nTo: Sam Organizer <{ORGANIZER}>\n"
           f"Subject: RE: Panelist info\n\nGAP: {GAP}\n\nBio: Lee leads the test unit.")
REPLY = (f"Room is set with chairs at the front for everyone, thanks.\n\nFrom: Owner <{ME}>\nSent: Sunday\nSubject: Re: Panelist info\n\n"
         "Old quoted text that the thread already holds.")


def _msg(mid, frm, subject, body, *, to=(ME,), received="2026-11-09T20:00:00Z", attachments=False):
    return {"id": mid, "conversationId": CONV, "subject": subject, "receivedDateTime": received,
            "isDraft": False, "hasAttachments": attachments,
            "from": {"emailAddress": {"name": frm.split("@")[0], "address": frm}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [], "body": {"contentType": "text", "content": body}}


class FakeGraph:
    MAIL_FIELDS = "id"
    MAIL_FIELDS_FULL = "id,body"

    def __init__(self, msgs):
        self.msgs = {m["id"]: m for m in msgs}
        self.attachment_reads = []

    def get_message_full(self, mid):
        return self.msgs[mid]

    def graph_get_all(self, path, params=None):
        return list(self.msgs.values())

    def get_attachments(self, mid):
        self.attachment_reads.append(mid)
        return []


class Chat:
    """The local model, stubbed: each call returns the next answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {"answer": a, "found": True, "messages": [1]}, {"latency_ms": 5}


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "state").mkdir()
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": ME}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(prefs, "PREFS_FILE", ea / "config" / "preferences.json")
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {ME})
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(CFG).encode(), mode=0o600)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    pr.reset()
    return d


def _thread(officer_in_thread=True):
    first = _msg(MID1, ORGANIZER, "Panelist info", "Please send your gap by Nov 11.",
                 to=(OFFICER, ME) if officer_in_thread else (ME,), received="2026-11-06T20:00:00Z")
    fwd = _msg(MID2, ORGANIZER, "Fw: Panelist info", FORWARD, received="2026-11-09T23:00:00Z")
    reply = _msg(MID3, ORGANIZER, "Re: Panelist info", REPLY, received="2026-11-10T20:00:00Z")
    return [first, fwd, reply]


# ── what the model reads ──────────────────────────────────────────────

def test_a_forward_is_read_whole_and_a_reply_without_its_quoted_history(env):
    graph, chat = FakeGraph(_thread()), Chat("Lee's gap is about trusting one picture.")
    out = mail_ask.ask("What gap did Lee submit?", conversation_id=CONV, graph=graph, chat=chat,
                       cfg=rl.config())
    prompt = chat.calls[0]["messages"][1]["content"]
    assert "conflicting reports from three sensor feeds" in prompt
    assert "Room is set" in prompt and "Old quoted text" not in prompt
    assert prompt.startswith("Question: What gap did Lee submit?")
    assert "=== Message 2 ===" in prompt and "Subject: Fw: Panelist info" in prompt
    assert out["answer"] == "Lee's gap is about trusting one picture."
    assert out["messages_read"] == 3 and out["read_by"] == "local model"
    assert out["sources"][0]["id"] == MID1
    assert chat.calls[0]["purpose"] == "mail-ask"


def test_one_message_is_read_whole(env):
    graph, chat = FakeGraph(_thread()), Chat("The room is set.")
    mail_ask.ask("Is the room set?", message_id=MID3, graph=graph, chat=chat, cfg=rl.config())
    assert "Old quoted text" in chat.calls[0]["messages"][1]["content"]


def test_a_long_thread_loses_its_oldest_text_first(monkeypatch):
    monkeypatch.setattr(mail_ask, "TOTAL_CHARS", 320)
    msgs = _thread()
    email, cut = mail_ask.compose(msgs, ["a" * 200, "b" * 200, "c" * 50])
    assert cut and "a" * 200 not in email and "b" * 200 in email and "c" * 50 in email


# ── keep levels ───────────────────────────────────────────────────────

def test_a_thread_with_a_listed_participant_is_answered_in_own_words_without_files(env):
    msgs = _thread()
    msgs[1]["hasAttachments"] = True
    graph, chat = FakeGraph(msgs), Chat("Lee wants one trusted picture.")
    out = mail_ask.ask("What gap did Lee submit?", conversation_id=CONV, graph=graph, chat=chat,
                       cfg=rl.config())
    assert out["kept_level"] == pr.plain("local")
    assert out[mp.KEPT_FIELD] == "listed domain mil"
    assert mail_ask.QUOTE_RULES["own"] in chat.calls[0]["messages"][0]["content"]
    assert graph.attachment_reads == []


def test_ordinary_mail_may_quote_and_its_files_are_read(env):
    msgs = _thread(officer_in_thread=False)
    msgs[1]["body"]["content"] = FORWARD.replace(OFFICER, "lee@partner.example.org")
    msgs[1]["hasAttachments"] = True
    graph, chat = FakeGraph(msgs), Chat("Lee wrote: one picture.")
    out = mail_ask.ask("What gap did Lee submit?", conversation_id=CONV, graph=graph, chat=chat,
                       cfg=rl.config())
    assert "kept_level" not in out
    assert mail_ask.QUOTE_RULES["may_quote"] in chat.calls[0]["messages"][0]["content"]
    assert graph.attachment_reads == [MID2]


def test_facts_only_mail_answers_nothing(env, monkeypatch):
    monkeypatch.setattr(mail_ask, "decide", lambda msgs, conv="", cfg=None:
                        {"level": "facts", "reason": "your rule", "files_kept": True})
    chat = Chat("anything")
    out = mail_ask.ask("What gap?", conversation_id=CONV, graph=FakeGraph(_thread()), chat=chat,
                       cfg=rl.config())
    assert out["error"] == mail_ask.FACTS_REFUSAL and chat.calls == []


# ── the verbatim check ────────────────────────────────────────────────

def test_a_copied_run_is_asked_again_in_own_words(env):
    copied = "Lee said operators get conflicting reports from three sensor feeds and need one picture."
    chat = Chat(copied, "Lee wants a single trusted view built from several disagreeing sensors.")
    out = mail_ask.ask("What gap?", conversation_id=CONV, graph=FakeGraph(_thread()), chat=chat,
                       cfg=rl.config())
    assert len(chat.calls) == 2
    assert "Answer again in your own words" in chat.calls[1]["messages"][-1]["content"]
    assert out["answer"].startswith("Lee wants a single trusted view")


def test_a_model_that_keeps_copying_is_refused(env):
    copied = "Operators get conflicting reports from three sensor feeds and need one picture they can trust."
    out = mail_ask.ask("What gap?", conversation_id=CONV, graph=FakeGraph(_thread()),
                       chat=Chat(copied, copied), cfg=rl.config())
    assert "quoted the mail" in out["error"]
    assert copied not in json.dumps(out)


def test_copied_run_finds_twelve_words_and_ignores_shorter_overlap():
    src = "one two three four five six seven eight nine ten eleven twelve thirteen"
    assert mail_ask.copied_run("x one two three four five six seven eight nine ten eleven twelve", src)
    assert mail_ask.copied_run("one two three four five six seven eight nine ten eleven", src) == ""


# ── arguments, the CLI and the mediator ───────────────────────────────

def test_the_question_and_one_target_are_required():
    assert "empty" in mail_ask.ask("  ", conversation_id=CONV, cfg=CFG)["error"]
    assert "one of" in mail_ask.ask("q", cfg=CFG)["error"]
    assert "one of" in mail_ask.ask("q", message_id=MID1, conversation_id=CONV, cfg=CFG)["error"]
    assert mail_ask.ask("q", message_id=MID1, cfg={})["need"] == "no local model"


def test_the_cli_reads_outlook_only():
    args = mail_actions.build_parser().parse_args(
        ["ask", "--provider", "gmail", "--message-id", "18f0", "--question", "What gap?"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 1 and "Outlook" in json.loads(buf.getvalue())["error"]
    with pytest.raises(SystemExit):
        mail_actions.build_parser().parse_args(["ask", "--provider", "outlook", "--question", "q"])


def test_the_verb_is_a_mediated_read_on_its_own_thread():
    assert MM.is_safe("mail_actions", "ask")
    assert not MM.is_gated("mail_actions", "ask")
    ok = ["ask", "--provider", "outlook", "--conversation-id", CONV, "--question", "What gap?"]
    assert MM.sanitize("mail_actions", "ask", ok) is None
    bad = ["ask", "--provider", "outlook", "--conversation-id", "AAQk/../x", "--question", "q"]
    assert "plain mail id" in MM.sanitize("mail_actions", "ask", bad)
    assert MM.sanitize("mail_actions", "ask", ok[:-2] + ["--quest", "q"])
    assert MM.draft_lock_scope("mail_actions", "ask", ok) is None
    assert ("mail_actions", "ask") in mail_mediator.ASYNC_VERBS
    assert cg.is_ghost_read("mail_actions", "ask")


def test_a_cron_lane_asks_nothing_about_a_kept_thread(monkeypatch):
    index = {"by_id": {MID2: {"id": MID2, "conversation_id": CONV, "keep_from_frontier": "domain"}},
             "by_conv": {CONV: [{"id": MID2, "keep_from_frontier": "domain"}]}, "by_key": {}}
    monkeypatch.setattr(mp, "load_index", lambda: index)
    monkeypatch.setattr(mp.remote_llm, "enforcing", lambda part="mail", cfg=None: True)
    by_conv = ["ask", "--provider", "outlook", "--conversation-id", CONV, "--question", "q"]
    by_msg = ["ask", "--provider", "outlook", "--message-id", MID2, "--question", "q"]
    assert mp.release_precheck("mail_actions", "ask", by_conv, "cron-brief-1") == mp.KEPT_REFUSAL
    assert mp.release_precheck("mail_actions", "ask", by_msg, "cron-brief-1") == mp.KEPT_REFUSAL
    assert mp.release_precheck("mail_actions", "ask", by_conv, "room-run-1") == ""
    # the answer is the local model's: the release filter leaves it as it is
    out = json.dumps({"question": "q", "answer": "a"})
    assert mp.release_filter("mail_actions", "ask", by_conv, "room-run-1", out) == (out, "")
