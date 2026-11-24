#!/usr/bin/env python3
"""Tests for preprocessor/style_guard.py + diction-rule wiring pins.

The dash rewrite covers only Outlook draft bodies, so Slack reply text
needs its own guard:
  - strip_em_dashes() is the deterministic backstop on all Slack output
  - the resident dispatch protocol injects STYLE_RULES per message
"""

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

from style_guard import strip_em_dashes  # noqa: E402

BOT_SRC = (HERE.parent / "ea-chat-bot.py").read_text()


def test_em_dash_joint_becomes_comma():
    out, n = strip_em_dashes("The PSR is in — approvals are next.")
    assert out == "The PSR is in, approvals are next."
    assert n == 1


def test_em_dash_no_spaces():
    out, n = strip_em_dashes("deadline—June 19")
    assert out == "deadline, June 19"
    assert n == 1


def test_double_hyphen_joint():
    out, n = strip_em_dashes("it works -- mostly.")
    assert out == "it works, mostly."
    assert n == 1


def test_en_dash_ranges_preserved():
    # Briefings legitimately use en-dash ranges; only em-dash is banned.
    text = "Panel 7:00 AM – 1:00 PM, Jun 19–30 window"
    out, n = strip_em_dashes(text)
    assert out == text
    assert n == 0


def test_code_spans_untouched():
    text = "Run `graph_actions.py --body x — test` now — really."
    out, n = strip_em_dashes(text)
    assert "`graph_actions.py --body x — test`" in out
    assert out.endswith("now, really.")
    assert n == 1


def test_fenced_block_untouched():
    text = "```\na — b\n```\noutside — text"
    out, n = strip_em_dashes(text)
    assert "a — b" in out
    assert "outside, text" in out
    assert n == 1


def test_cli_double_dash_flags_untouched():
    text = "use --body-file and --conversation-id"
    out, n = strip_em_dashes(text)
    assert out == text and n == 0


def test_clean_text_fast_path():
    out, n = strip_em_dashes("no dashes here at all")
    assert n == 0


def test_multiple_em_dashes():
    out, n = strip_em_dashes("a — b — c")
    assert out == "a, b, c"
    assert n == 2


# ---------------------------------------------------------------------------
# Wiring pins (source-level): the diction rules must reach every lane
# ---------------------------------------------------------------------------

def test_resident_protocol_injects_style_rules():
    """The resident must carry STYLE_RULES + per-recipient style guidance.
    They live in resident_context.PRIMER, injected once per session by
    build_dispatch, not inline in _try_resident_dispatch."""
    # The dispatch path must route through the smart-context builder.
    start = BOT_SRC.index("def _try_resident_dispatch")
    end = BOT_SRC.index("def handle_message_async")
    block = BOT_SRC[start:end]
    assert "build_dispatch" in block, \
        "resident dispatch no longer routes through resident_context.build_dispatch"
    # The PRIMER must embed STYLE_RULES and the per-recipient lookup.
    import resident_context as rcx
    from prompt_logic import STYLE_RULES
    assert STYLE_RULES.strip()[:60] in rcx.PRIMER, \
        "PRIMER lost its STYLE_RULES embedding"
    assert "style_profiles.py" in rcx.PRIMER and "--contact" in rcx.PRIMER, \
        "PRIMER lost the per-recipient style lookup"


def test_scrub_wired_into_all_slack_output_paths():
    for fn, needle in [
        ("_try_resident_dispatch", "strip_em_dashes"),
        ("resident_router_loop", "strip_em_dashes"),
        ("handle_message_async", "strip_em_dashes"),
    ]:
        start = BOT_SRC.index(f"def {fn}")
        # the whole function: up to the next column-0 def (nested defs are indented)
        end = BOT_SRC.find("\ndef ", start + 1)
        block = BOT_SRC[start:end if end > 0 else len(BOT_SRC)]
        assert needle in block, f"{fn} lost its style_guard scrub"


def test_style_rules_still_ban_em_dashes():
    from prompt_logic import STYLE_RULES
    assert "em-dash" in STYLE_RULES.lower() or "em dash" in STYLE_RULES.lower()
    # Per-recipient voice matching is driven by the style_profiles DB.
    assert "style_profiles.py" in STYLE_RULES and "--contact" in STYLE_RULES


# ── where a card's button is: said once, by the bot's pointer ─────────

DRAFT_ID = "AAMkAGZmYWtlLWlkLWZvci10aGUtdGVzdHMtb25seQBGAAAAAAB="


def test_the_reply_drops_its_own_sentence_about_where_the_send_button_is():
    from style_guard import strip_card_pointers as strip
    for text, want in [
        (":email: Draft created to Pat re: Budget.\nThe Send button is on the card in Action requests.",
         ":email: Draft created to Pat re: Budget."),
        ("Both drafts are ready. The **Send** and **Create** buttons are on the cards in **Action requests**.",
         "Both drafts are ready."),
        ("Each Send button is on its card in Action requests.\n\nDone.", "Done."),
        ("I drafted the reply; the Send button is in your DM.", "I drafted the reply."),
        ("Accepted Tuesday 3 PM, and the Create and Send buttons are in Sam's DM.", "Accepted Tuesday 3 PM."),
        (":email: I made a reply draft to Pat. The Send button is in Action requests. It offers Tuesday.",
         ":email: I made a reply draft to Pat. It offers Tuesday."),
    ]:
        assert strip(text)[0] == want, text
    # a sentence with words of its own about the card stays, and so does code
    for text in ("The card in Action requests shows the old body, so I revised it.",
                 "I kept the draft (the Send button is in Action requests) as it was.",
                 "Use `The Send button is in Action requests.` as the test line.",
                 "Nothing to send today."):
        assert strip(text) == (text, 0), text


def test_a_turn_line_in_action_requests_names_no_tool_and_no_full_id():
    from style_guard import plain_turn_text as plain_text
    for text, want in [
        (f":email: `create-reply` created the reply-all draft with two questions. Draft ID: `{DRAFT_ID}`",
         ":email: Created the reply-all draft with two questions."),
        (f":email: Draft created to Pat, ID `{DRAFT_ID}`.", ":email: Draft created to Pat."),
        ("Recorded the answer with `ledger.py note-reply`. No draft created.",
         "Recorded the answer. No draft created."),
        ("1. :email: `update-draft` added the second date.\n2. `mail_actions.py ask` read the forward: no request.",
         "1. :email: Added the second date.\n2. Read the forward: no request."),
        (f"Done (id `{DRAFT_ID}`).\n\nDraft ID: `{DRAFT_ID}`", "Done."),
    ]:
        assert plain_text(text)[0] == want, text
    for text in ("Run `ledger.py list` to see the rows.", "Nothing to change.", "Created draft `m3` to Lee."):
        assert plain_text(text) == (text, 0), text


def test_the_bot_scrubs_each_turn_reply_and_keeps_its_words_for_the_promise_check():
    if str(HERE.parent) not in sys.path:  # the bot imports preprocessor.style_guard
        sys.path.insert(0, str(HERE.parent))
    ns = {"log": type("L", (), {"info": staticmethod(lambda *a: None),
                                "warning": staticmethod(lambda *a: None)})}

    class FE:
        dm_takes_text = False

        def is_dm(self, channel):
            return channel == "dm"
    ns["FRONTEND"] = FE()
    ns["_reply_taps_only"] = lambda: ns["FRONTEND"].dm_takes_text is False
    start = BOT_SRC.index("def _scrub_turn_reply(")
    exec(BOT_SRC[start:BOT_SRC.index("\ndef ", start + 1)], ns)
    scrub = ns["_scrub_turn_reply"]
    said = f"`create-reply` created the draft. Draft ID: `{DRAFT_ID}`\nThe Send button is on the card in Action requests."
    assert scrub("dm", said) == "Created the draft."
    assert scrub("main", said) == f"`create-reply` created the draft. Draft ID: `{DRAFT_ID}`"
    ns["FRONTEND"].dm_takes_text = True                  # Slack's DM: no tool scrub
    assert scrub("dm", said) == f"`create-reply` created the draft. Draft ID: `{DRAFT_ID}`"
    assert scrub("main", "The Send button is in your DM.") == "The Send button is in your DM."  # never empty
    block = BOT_SRC[BOT_SRC.index("def handle_message_async"):]
    assert "said_before_scrub = response\n        response = _scrub_turn_reply(channel, response)" in block
    assert '_warn_if_button_promised(channel, thread_ts, said_before_scrub, "mono")' in block


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
