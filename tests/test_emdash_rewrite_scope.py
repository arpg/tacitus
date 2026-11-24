#!/usr/bin/env python3
"""The em-dash auto-rewrite may only touch the EA's own text.

Fetching the draft with --raw-html hands `_auto_rewrite_remove_dashes`
the new text PLUS the whole quoted thread. Rewriting all of it has two
consequences:

  * other people's quoted words are altered, and the 80% length guard is
    measured against the whole document, so a long thread can absorb the
    loss of most of the reply;
  * the :lock: block renders only the portion above the quote marker, so
    the reviewed text is not the sent text.

Both halves are pinned here: the rewrite is scoped to the new portion, and
a rewrite that lands after a block is already up refreshes that block.
"""

import json
import re
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import atomic_io as _atomic  # noqa: E402

import chat as _chat  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
# One scratch dir for the pure-function tests; the behavioural
# ones take their own TemporaryDirectory.
_TMPROOT = Path(tempfile.mkdtemp(prefix="ea-emdash-tests-"))


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    return BOT_SRC[start:end]


# A reply as Outlook stores it: EA text at the top, then the thread.
QUOTED = (
    '<div id="divRplyFwdMsg"><font face="Calibri">'
    "<b>From:</b> Oakley &lt;oakley@example.com&gt;<br></font></div>"
    "<div>The results — surprisingly — held.</div></body></html>"
)
REPLY_HTML = "<html><head></head><body><div>Numbers Friday — promise.</div>"
FULL_HTML = REPLY_HTML + "<hr>" + QUOTED


def _rewrite_ns(tmp: Path):
    ns = {
        "re": re, "json": json, "os": __import__("os"), "_atomic": _atomic,
        "EA_DIR": tmp, "SCRIPT_DIR": ROOT, "EA_BOT_HOME": str(tmp),
        # The body file goes to the owner-private scratch dir.
        "_private_tmp": lambda: tmp / "state",
        "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                     debug=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
    }
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    _exec_block("_EMDASH_ERROR_FRAGMENT", "def _run_post_draft_hook", ns)
    return ns


# ---------------------------------------------------------------------------
# the splitter
# ---------------------------------------------------------------------------

def test_split_finds_the_outlook_header_block():
    ns = _rewrite_ns(_TMPROOT)
    prefix, new, quoted = ns["_split_new_from_quoted"](FULL_HTML, "html")
    assert prefix + new + quoted == FULL_HTML
    assert "Numbers Friday" in new
    assert "surprisingly" not in new
    assert "surprisingly" in quoted
    assert prefix.endswith("<body>"), prefix
    assert quoted.startswith("<hr>"), (
        "cut ABOVE the separator, the way graph_client._split_quoted_thread "
        "does, or update-draft re-inserts the fragment below its own "
        "boundary and the page ends up with two rules")
    assert "<hr" not in new


def test_split_uses_the_ea_markers_when_they_survived():
    body = ("<html><body><!-- EA_REPLY_START --><div>Mine — here.</div>"
            "<!-- EA_REPLY_END --><br>" + QUOTED)
    ns = _rewrite_ns(_TMPROOT)
    prefix, new, quoted = ns["_split_new_from_quoted"](body, "html")
    assert prefix + new + quoted == body
    assert new.strip() == "<div>Mine — here.</div>"
    assert quoted.startswith("<!-- EA_REPLY_END -->")


def test_split_handles_a_plain_text_reply():
    body = "Hi Oakley — Friday works.\n\nFrom: Oakley\nThe results — held."
    ns = _rewrite_ns(_TMPROOT)
    prefix, new, quoted = ns["_split_new_from_quoted"](body, "text")
    assert prefix == ""
    assert new.startswith("Hi Oakley")
    assert quoted.startswith("\nFrom:")


def test_split_of_a_body_with_no_quote_is_the_whole_body():
    ns = _rewrite_ns(_TMPROOT)
    assert ns["_split_new_from_quoted"]("Hi — there", "text") == \
        ("", "Hi — there", "")


# ---------------------------------------------------------------------------
# the rewrite
# ---------------------------------------------------------------------------

def _run_rewrite(ns, body, body_type):
    calls = []
    captured = {}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        r = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        if "get-message" in cmd:
            r.stdout = json.dumps({"body": body, "body_type": body_type})
        else:
            if "update-draft" in cmd:
                path = cmd[cmd.index("--body-file") + 1]
                captured["body"] = Path(path).read_text(encoding="utf-8")
            r.stdout = "{}"
        return r

    with patch("subprocess.run", side_effect=fake_run):
        ok, status = ns["_auto_rewrite_remove_dashes"]("draft-1")
    return ok, status, calls, captured


def _no_model(*a, **k):
    raise AssertionError("no model call expected")


def test_the_remote_model_never_sees_the_quoted_thread():
    """When the fixed rules leave a dash, only the new text goes to the
    remote model."""
    with tempfile.TemporaryDirectory() as td:
        ns = _rewrite_ns(Path(td))
        prompts = []
        ns["_replace_dashes"] = lambda t: t
        ns["_remote_text"] = lambda prompt, **kw: prompts.append(prompt) or \
            "<html><head></head><body><div>Numbers Friday, promise.</div>"
        ok, status, calls, _ = _run_rewrite(ns, FULL_HTML, "html")
        assert ok, status
        assert prompts and "Numbers Friday" in prompts[0]
        assert "surprisingly" not in prompts[0], (
            "the quoted thread must never reach the rewriter")


def test_the_written_body_is_the_new_portion_only():
    """graph_client.update_draft re-assembles the reply around the thread it
    reads back, so handing it the whole document would duplicate the
    thread."""
    with tempfile.TemporaryDirectory() as td:
        ns = _rewrite_ns(Path(td))
        ns["_remote_text"] = _no_model
        ok, status, _, captured = _run_rewrite(ns, FULL_HTML, "html")
        assert ok, status
        assert "surprisingly" not in captured["body"]
        assert "divRplyFwdMsg" not in captured["body"], (
            "the quoted thread must not be written back")
        assert "Numbers Friday, promise." in captured["body"]


def test_a_dash_only_in_the_quoted_thread_is_not_our_business():
    with tempfile.TemporaryDirectory() as td:
        ns = _rewrite_ns(Path(td))
        ns["_remote_text"] = _no_model
        clean_reply = "<html><head></head><body><div>Numbers Friday.</div>"
        ok, status, calls, _ = _run_rewrite(ns, clean_reply + "<hr>" + QUOTED, "html")
        assert ok is False
        assert "no em/en dashes" in status
        assert not any("update-draft" in c for c in calls), (
            "no rewrite for someone else's dash")


def test_the_length_guard_is_measured_against_the_new_portion():
    """Against the whole body a long thread hides the loss of the reply."""
    with tempfile.TemporaryDirectory() as td:
        ns = _rewrite_ns(Path(td))
        ns["_replace_dashes"] = lambda t: t
        ns["_remote_text"] = lambda prompt, **kw: "<html>"
        long_quote = QUOTED + ("<div>earlier message</div>" * 400)
        body = REPLY_HTML + "<hr>" + long_quote
        ok, status, calls, _ = _run_rewrite(ns, body, "html")
        assert ok is False, status
        assert "lost too much content" in status
        assert not any("update-draft" in c for c in calls)


def test_residual_dash_substitution_stays_inside_the_new_text():
    with tempfile.TemporaryDirectory() as td:
        ns = _rewrite_ns(Path(td))
        ns["_remote_text"] = _no_model
        ok, _, _, captured = _run_rewrite(ns, FULL_HTML, "html")
        assert ok
        assert "\N{EM DASH}" not in captured["body"]
        assert "surprisingly" not in captured["body"]


# ---------------------------------------------------------------------------
# the confirmation block
# ---------------------------------------------------------------------------

def _confirm_ns(tmp: Path):
    frontend = MemoryFrontend()
    built = {"body": "rewritten body"}
    ns = {
        "json": json, "os": __import__("os"), "time": time, "_atomic": _atomic,
        "threading": __import__("threading"),
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "EA_DIR": tmp, "MAX_CONFIRM_AGE_HOURS": 48,
        "_private_state": lambda name: tmp / "state" / name,
        "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                     debug=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
        "_post_send_confirmation": lambda *a, **k: None,
        "_chat": _chat,
        "FRONTEND": frontend,
        "_one_list": lambda: False,
    }
    _exec_block("# Draft-confirmation registry",
                "def _replace_buttons_with_status", ns)

    def _build(channel, thread_ts, draft_id, *a, **k):
        # stands in for the card; the test reads it back from the update
        return {"body": built["body"]}, None

    ns["_build_send_confirmation"] = _build
    for fn in ("_card_ref", "_card_summary", "_refresh_send_confirmation"):
        exec(compile(_fn_src(fn), "ea-chat-bot.py[fn]", "exec"), ns)
    return ns, frontend.updates, built


def test_a_live_block_is_updated_after_a_rewrite():
    with tempfile.TemporaryDirectory() as td:
        ns, updates, built = _confirm_ns(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        built["body"] = "no dashes now"
        assert ns["_refresh_send_confirmation"]("AAMkD=") is True
        assert len(updates) == 1
        assert updates[0]["ref"].channel == "DM1"
        assert updates[0]["ref"].id == "111.1"
        assert updates[0]["card"] == {"body": "no dashes now"}, (
            "the reviewed text has to be the text that will be sent")


def test_a_retired_or_consumed_block_is_left_alone():
    with tempfile.TemporaryDirectory() as td:
        ns, updates, _ = _confirm_ns(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_retire_confirmation_message"]("DM1", "111.1")
        assert ns["_refresh_send_confirmation"]("AAMkD=") is False
        ns["_mark_draft_confirmation"]("AAMkD=", "consumed")
        assert ns["_refresh_send_confirmation"]("AAMkD=") is False
        assert updates == []


def test_an_unknown_draft_refreshes_nothing():
    with tempfile.TemporaryDirectory() as td:
        ns, updates, _ = _confirm_ns(Path(td))
        assert ns["_refresh_send_confirmation"]("AAMkNEVERSEEN=") is False
        assert updates == []


def test_the_hook_refreshes_the_block_after_a_successful_rewrite():
    body = _fn_src("_run_post_draft_hook")
    assert "_refresh_send_confirmation(draft_id)" in body, (
        "a rewrite that lands after the :lock: block is up leaves the "
        "reviewed text stale")


def test_the_poster_and_the_refresher_render_from_one_builder():
    post = _fn_src("_post_send_confirmation")
    refresh = _fn_src("_refresh_send_confirmation")
    assert "_build_send_confirmation(" in post
    assert "_build_send_confirmation(" in refresh, (
        "two renderers would drift; the refresh must reuse the poster's")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
