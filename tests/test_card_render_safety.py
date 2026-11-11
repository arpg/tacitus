#!/usr/bin/env python3
"""Card text the model or mail wrote renders as inert text.

A confirmation card shows a subject, display names, a location, plugin item
fields, a commit subject and the draft body, all of which the model or a
sender can write. These tests check that such text cannot forge a field
line, the card's rule or legend, a link whose text is not its target, or a
mention; that invisible and reordering characters are shown; that the
collapsed part of a body is only text that ends an earlier message; that
the Send-all card names what it sends; that a cut value withholds the
approval; and that the Schedule click takes the same claim as Send.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT))

import card_text as CT  # noqa: E402
import mail_links as ML  # noqa: E402
from chat import slack_frontend as S  # noqa: E402
from chat.base import ChatError  # noqa: E402
from chat.cards import CardAction, CardField, ConfirmationCard  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
QUIET = types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                              warning=lambda *a, **k: None, error=lambda *a, **k: None)


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    ends = [BOT_SRC.index("\ndef ", start + 1)]
    for deco in ("\n@app.action(", "\n# ----", "\n# ──"):
        nxt = BOT_SRC.find(deco, start + 1)
        if nxt > 0:
            ends.append(nxt)
    return BOT_SRC[start:min(ends)]


def _block(start: str, end: str) -> str:
    i = BOT_SRC.index(start)
    return BOT_SRC[i:BOT_SRC.index(end, i)]


# ── card_text ──────────────────────────────────────────────────────────

def test_clean_marks_invisible_and_reordering_characters():
    text = "pay\u202eetad\u200b \u2066x\u2069 \x00\x1b\x7f \ue000 \U000e0041 tab\tok\nline"
    out = CT.clean(text)
    for cp in ("202E", "200B", "2066", "2069", "0000", "001B", "007F", "E000", "E0041"):
        assert f"[U+{cp}]" in out, cp
    assert "\t" in out and "\n" in out
    assert CT.clean("a\r\nb\u2028c") == "a\nb\nc"
    # an emoji sequence keeps its joiner; the joiner alone is marked
    family = "\U0001F468\u200d\U0001F469"
    assert CT.clean(family) == family
    assert CT.clean("a\u200db") == "a[U+200D]b"


def test_one_line_folds_and_cuts_with_a_marker():
    assert CT.one_line("a\n\n  b\tc") == "a b c"
    cut = CT.one_line("x" * 500, 100)
    assert len(cut) <= 100 and cut.endswith("more characters]") and CT.was_cut(cut)
    assert not CT.was_cut(CT.one_line("short", 100))
    assert CT.field_limit("To") == CT.RECIPIENT_FIELD_CHARS
    assert CT.field_limit("Subject") == CT.FIELD_CHARS


def test_an_inert_value_cannot_close_its_own_span():
    span = CT.inert("a\ue001b\ue000c")
    assert span.count(CT.INERT_OPEN) == 1 and span.count(CT.INERT_CLOSE) == 1
    assert "[U+E001]" in span and "[U+E000]" in span


def test_code_span_cannot_be_closed_by_its_text():
    # the delimiter is one backtick longer than the longest run inside
    assert CT.code_span("a `b` ``c``") == "``` a `b` ``c`` ```"
    assert CT.code_span("plain") == "`plain`"
    assert CT.code_span("") == ""


def _send_card(**kw):
    base = dict(kind="send", title=":lock: *Confirm send*",
                fields=[CardField("To", "Pat <pat@example.org>"), CardField("Subject", "Re: x")],
                body="Hello.",
                actions=[CardAction("approve_send", ":outbox_tray: Send", {"d": 1}, "primary",
                                    "send"),
                         CardAction("cancel_send", "Cancel", {"d": 1}, "danger", "cancel")])
    base.update(kw)
    return ConfirmationCard(**base)


# ── Slack cards ────────────────────────────────────────────────────────

def test_slack_escapes_fields_body_and_inert_spans():
    card = _send_card(
        title=":lock: *Publish* " + CT.inert("<!channel> <https://evil.example|ok>"),
        fields=[CardField("To", "Pat <pat@example.org>\n*CC:* boss@example.org"),
                CardField("Subject", "<@U999> hi")],
        body="See <https://evil.example|your invoice> & <!here>")
    text, blocks = S.render_card(card)
    flat = json.dumps(blocks)
    for bad in ("<!channel>", "<!here>", "<@U999>", "<https://evil.example|"):
        assert bad not in flat and bad not in text, bad
    fields = next(b for b in blocks if "fields" in b)["fields"]
    assert fields[0]["text"] == "*To:* Pat &lt;pat@example.org&gt; *CC:* boss@example.org"
    assert any("&lt;https://evil.example|your invoice&gt; &amp; &lt;!here&gt;" in
               (b.get("text") or {}).get("text", "") for b in blocks)


def test_slack_start_refuses_an_owner_id_equal_to_the_bots():
    client = types.SimpleNamespace(auth_test=lambda: {"user_id": "U_BOT"})
    fe = S.SlackFrontend("xoxb", channel_name="ea", channel_id="C1", owner_id="U_BOT",
                         for_cli=True, client=client)
    with pytest.raises(ChatError, match="bot's own user id"):
        fe.start()


# ── the collapsed part of a body ────────────────────────────────────────

def test_verified_quote_start_needs_text_that_ends_an_earlier_message():
    own = "Thanks, see you Monday.\n"
    header = "From: Pat\nSent: Friday\nSubject: Re: x\n\n"
    prior = "Can we meet\nMonday at 10?\n\nPat"
    text = own + header + prior.replace("\n", "\n ")
    start = len(own)
    cut = ML.verified_quote_start(text, start, ["unrelated", prior])
    assert text[cut:].lstrip().startswith("Can we meet") and "Sent:" in text[:cut]
    # text added after the earlier message keeps everything in view
    assert ML.verified_quote_start(text + "\nP.S. wire it today.", start, [prior]) == \
        len(text) + len("\nP.S. wire it today.")
    # a fragment from inside an earlier message is not its end
    assert ML.verified_quote_start(own + header + "Can we meet", start, [prior]) == \
        len(own + header + "Can we meet")
    assert ML.verified_quote_start(text, start, []) == len(text)
    assert ML.verified_quote_start(text, len(text), [prior]) == len(text)
    # quote marks and rewrapping do not matter
    gm = own + "On Fri, Pat wrote:\n> Can we meet Monday\n> at 10?\n>\n> Pat\n"
    assert gm[ML.verified_quote_start(gm, len(own), [prior]):].startswith("> Can we meet")


# ── the bot's card builders ──────────────────────────────────────────────

def _cards_ns(fits=True):
    from preprocessor import chat as _chat
    import dataclasses
    ns = {"json": json, "_chat": _chat, "dataclasses": dataclasses,
          "FRONTEND": types.SimpleNamespace(card_fits=lambda card: fits)}
    exec(compile(_block("# ── Confirmation cards", "# ── End confirmation cards"),
                 "bot[cards]", "exec"), ns)
    return ns


def _draft(**kw):
    d = {"to": "pat@example.org", "cc": "", "bcc": "", "subject": "Re: budget",
         "body": "ok", "attachments": [], "fingerprint": "sha256:aa"}
    d.update(kw)
    return d


def test_a_gmail_card_names_the_from_account():
    ns = _cards_ns()
    card = ns["_send_card"]("C1", "T1", "r-1", _draft(**{"from": "Kit <kit@example.com>"}),
                            "", "gmail", "kit@example.com")
    assert ("From", "Kit <kit@example.com>") in [(f.label, f.value) for f in card.fields]
    card = ns["_send_card"]("C1", "T1", "r-1", _draft(), "", "gmail", "kit@example.com")
    assert ("From", "kit@example.com") in [(f.label, f.value) for f in card.fields]
    card = ns["_send_card"]("C1", "T1", "AAMk=", _draft(), "", "outlook", "")
    assert "From" not in [f.label for f in card.fields]


def test_a_field_the_card_cuts_withholds_send_and_schedule():
    ns = _cards_ns()
    long_to = ", ".join(f"p{i}@example.org" for i in range(200))
    card = ns["_send_card"]("C1", "T1", "AAMk=", _draft(to=long_to), "", "outlook", "")
    assert "approve_send" not in [a.id for a in card.actions]
    assert any("the To line is longer" in ln for ln in card.lines)
    card = ns["_schedule_card"]("C1", "T1", "AAMk=", _draft(subject="s" * 400),
                                "2026-10-20T09:00", "tomorrow")
    assert [a.id for a in card.actions] == ["cancel_schedule_send"]
    ok = ns["_send_card"]("C1", "T1", "AAMk=", _draft(), "", "outlook", "")
    assert ok.actions[0].id == "approve_send"


def test_the_send_all_card_lists_each_drafts_recipients_and_subject():
    ns = _cards_ns()
    sums = [{"To": "pat@example.org", "Subject": "Re: budget"},
            {"To": "lee@example.org", "CC": "kim@example.org", "Subject": "<!here> Notes"}]
    card = ns["_bulk_card"]("b1", 2, sums)
    assert len(card.lines) == 2
    z = CT.strip_inert("\n".join(card.lines))
    assert "pat@example.org" in z and "Re: budget" in z and "kim@example.org" in z
    _t, blocks = S.render_card(card)
    flat = json.dumps(blocks)
    assert "&lt;!here&gt; Notes" in flat and "<!here>" not in flat


def _registry_ns(td):
    try:
        import atomic_io as _atomic
    except ModuleNotFoundError:
        from preprocessor import atomic_io as _atomic
    ns = {"json": json, "os": os, "time": time, "_atomic": _atomic, "threading": threading,
          "EA_DIR": Path(td), "MAX_CONFIRM_AGE_HOURS": 48, "log": QUIET,
          "_private_state": lambda name: Path(td) / "state" / name,
          "_queue_bulk_candidate": lambda *a, **k: None}
    (Path(td) / "state").mkdir(exist_ok=True)
    exec(compile(_block("# Draft-confirmation registry", "def _replace_buttons_with_status"),
                 "bot[registry]", "exec"), ns)
    return ns


def test_the_send_all_card_binds_the_fingerprints_it_lists():
    with tempfile.TemporaryDirectory() as td:
        ns = _registry_ns(td)
        ns.update(_cards_ns())
        posted, batches = [], {}
        ns.update({"_BULK_LOCK": threading.Lock(), "uuid": __import__("uuid"),
                   "_BULK_PENDING": {"ids": ["d1", "d2"], "timer": None, "dm_channel": "DM"},
                   "_load_bulk_batches": lambda: json.loads(json.dumps(batches)),
                   "_save_bulk_batches": lambda d: (batches.clear(), batches.update(d)),
                   "_post_card": lambda ch, card: (posted.append(card),
                                                   types.SimpleNamespace(id="9.9"))[1]})
        exec(compile(_fn_src("_post_bulk_card"), "bot[bulk]", "exec"), ns)
        note = ns["_note_draft_confirmation"]
        note("d1", "DM", "1.1", "C1", "", fingerprint="fp1",
             summary={"To": "pat@example.org", "Subject": "A"})
        note("d2", "DM", "1.2", "C1", "", fingerprint="fp2",
             summary={"To": "lee@example.org", "Subject": "B"})
        ns["_post_bulk_card"]()
        assert len(posted) == 1 and len(posted[0].lines) == 2
        batch = next(iter(batches.values()))
        assert batch["fingerprints"] == {"d1": "fp1", "d2": "fp2"}
        # a draft with no recorded summary (an older row) gets no Send-all card
        posted.clear()
        note("d2", "DM", "1.2", "C1", "", fingerprint="fp2")
        ns["_BULK_PENDING"]["ids"] = ["d1", "d2"]
        ns["_post_bulk_card"]()
        assert posted == []
    h = _fn_src("handle_approve_bulk_send")
    assert 'listed.get(draft_id) != fingerprint' in h and "fingerprint=fingerprint" in h


def test_a_schedule_card_is_not_offered_to_send_all():
    src = _fn_src("_post_schedule_send_confirmation")
    assert "sendable=False" in src


def test_a_double_schedule_click_runs_once():
    with tempfile.TemporaryDirectory() as td:
        ns = _registry_ns(td)
        runs, statuses = [], []
        ns.update({
            "_verify_button_user": lambda ev: True,
            "_reject_if_stale_confirmation": lambda ev, label: False,
            "_card_value": lambda ev: ev.value,
            "_replace_buttons_with_status": lambda ch, ts, text: statuses.append(text),
            "_execute_schedule_send": lambda *a, **k: runs.append(a),
            "threading": types.SimpleNamespace(
                Thread=lambda target, daemon: types.SimpleNamespace(start=target)),
        })
        exec(compile(_fn_src("handle_approve_schedule_send"), "bot[sched]", "exec"), ns)
        ns["_note_draft_confirmation"]("AAMk=", "DM", "5.5", "C1", "T1", fingerprint="fp",
                                       sendable=False)
        ev = types.SimpleNamespace(
            user_id="U_OWNER", card=types.SimpleNamespace(channel="DM", id="5.5"),
            value={"thread_ts": "T1", "draft_id": "AAMk=", "send_after": "2026-10-20T09:00",
                   "ea_channel": "C1"})
        ns["handle_approve_schedule_send"](ev)
        ns["handle_approve_schedule_send"](ev)
        assert len(runs) == 1
        assert statuses[-1].startswith(":hourglass_flowing_sand: Already scheduling")


def test_the_bots_own_id_never_passes_the_owner_check():
    ns = {"OWNER_SLACK_USER_ID": "U_BOT", "log": QUIET,
          "FRONTEND": types.SimpleNamespace(bot_user_id="U_BOT"),
          "_post_ownerless_refusal": lambda ev: None}
    exec(compile(_fn_src("_verify_button_user"), "bot[verify]", "exec"), ns)
    assert ns["_verify_button_user"](types.SimpleNamespace(user_id="U_BOT")) is False
    ns["OWNER_SLACK_USER_ID"] = "U_OWNER"
    assert ns["_verify_button_user"](types.SimpleNamespace(user_id="U_OWNER")) is True


def test_a_plugin_card_with_a_cut_value_offers_no_approval():
    import plugin_manifest as PM
    from preprocessor import chat as _chat
    norm, _ = PM.load(ROOT / "tests" / "fixtures" / "plugins" / "example-notes")
    spec = norm["capabilities"]["cards"][0]
    LP = namedtuple("LP", ["name"])
    cards = []
    ns = {"_chat": _chat, "log": QUIET,
          "_plugin_card_entry": lambda action_type: (LP("example-notes"), spec),
          "FRONTEND": types.SimpleNamespace(post=lambda **k: None),
          "_deliver_confirmation": lambda ch, th, card, noun, kind: cards.append(card)}
    exec(compile(_fn_src("_post_plugin_card"), "bot[plugin]", "exec"), ns)
    ns["_post_plugin_card"]("C1", "T1", "publish-note", {"title": "t" * 400, "summary": "s"})
    ns["_post_plugin_card"]("C1", "T1", "publish-note", {"title": "short", "summary": "s"})
    cut, whole = cards
    assert [a.id for a in cut.actions] == ["plugin_card_cancel"]
    assert any("title is longer than a card shows" in ln for ln in cut.lines)
    assert [a.id for a in whole.actions] == ["plugin_card_approve", "plugin_card_cancel"]


def test_publish_card_commit_text_is_inert():
    import publish_gate as PG
    req = {"target": "site", "name": "demo", "commit": "a" * 40}
    pv = {"commit": "a" * 40, "base": "b" * 40, "subject": "fix `x` <!here> [a](https://e.x)",
          "files": [{"path": "a`b.md", "change": "modified"}], "url": "https://e.example"}
    scan = {"counts": {"email address": 1},
            "findings": [{"file": "a.md", "line": 3, "kind": "email address",
                          "match": "x@y.org`\n----"}]}
    r = PG.render(req, pv, scan)
    card = ConfirmationCard(kind="publish", title=r["title"], lines=r["lines"],
                            actions=[CardAction("approve_publish", "Publish", {}, "primary",
                                                "publish")])
    # every model or repository value is an inert span on one line, which
    # the web chat draws as literal text
    assert all("\n" not in ln for ln in card.lines)
    for value in ("fix `x` <!here> [a](https://e.x)", "a`b.md", "x@y.org` ----"):
        assert any(f"{CT.INERT_OPEN}{value}{CT.INERT_CLOSE}" in ln for ln in card.lines), value
    assert "<!here>" not in json.dumps(S.render_card(card)[1])
