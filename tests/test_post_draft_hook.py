#!/usr/bin/env python3
"""
Unit tests for the post-draft hook em-dash auto-rewrite flow in
ea-chat-bot.py.

The bot's module-level code reads env vars and validates the ea-bot user.
We stub the env vars for the duration of the import; on developer/CI
hosts where ea-bot is not provisioned, the tests skip.

Env setup lives in a module-scoped fixture, and the scoping is
load-bearing. pytest imports every test module during collection, so a
module-level `os.environ["HOME"] = ...` lands before any test in the
session runs and stays there. Subprocess tests in other files inherit the fake
HOME, which hides ~/.local/lib/pythonX/site-packages from the child and
breaks their imports (tests/test_gmail_client.py shells out to
gmail_client.py and lost `google.auth` this way).
"""

import importlib.util
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Bot imports `from preprocessor.prompt_logic import ...`, which resolves
# relative to cwd, but pytest/__main__ runs from `tests/`. Put project
# root on path so the import works.
sys.path.insert(0, str(PROJECT_ROOT))

# Populated by the _bot_module fixture (pytest) or the standalone runner.
_bot = None


def _ea_bot_present() -> bool:
    try:
        import pwd
        pwd.getpwnam("ea-bot")
    except KeyError:
        return False
    return True


def _bot_env(home: Path) -> dict:
    """Env the bot's module-level code needs to import cleanly.

    HOME points at a scratch dir for two reasons: the bot does
    `(Path.home() / ".ea" / "logs").mkdir(...)` at import, and it merges
    `~/.ea/.env.secrets` into os.environ. A real HOME would both fail the
    mkdir (/home/ea-bot/.ea is unreachable from the test runner) and pull
    live Slack tokens into the test process.
    """
    return {
        "HOME": str(home),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "EA_SLACK_CHANNEL_NAME": "ai-assistant-test",
        "EA_SLACK_CHANNEL_ID": "C00000TEST",
        # The card registry lives in the private dir; keep the test's copy
        # in the scratch home, off the real /home/crh/.ea-private.
        "EA_PRIVATE_DIR": str(home / ".ea-private"),
    }


def _prepare_home(home: Path) -> None:
    # The auto-rewrite function's tempfile.NamedTemporaryFile writes into
    # EA_DIR / "state", which the bot never creates itself.
    (home / ".ea" / "state").mkdir(parents=True, exist_ok=True)


def _import_bot():
    """Load ea-chat-bot.py (hyphenated filename, so load via spec).

    slack_bolt.App and SocketModeHandler are stubbed for the duration:
    a real `App(token=...)` makes an auth.test call against Slack, and
    the test token is fake.
    """
    import slack_bolt
    import slack_bolt.adapter.socket_mode as _sm

    real_app_cls = slack_bolt.App
    real_handler_cls = _sm.SocketModeHandler
    slack_bolt.App = MagicMock(name="StubApp")
    _sm.SocketModeHandler = MagicMock(name="StubSocketModeHandler")
    try:
        spec = importlib.util.spec_from_file_location(
            "ea_chat_bot", str(PROJECT_ROOT / "ea-chat-bot.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["ea_chat_bot"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore so other tests in the same process see the real classes.
        slack_bolt.App = real_app_cls
        _sm.SocketModeHandler = real_handler_cls


@pytest.fixture(scope="module", autouse=True)
def _bot_module(tmp_path_factory):
    """Import the bot with a scratch HOME, then put the env back.

    The env only has to hold while the module executes; the bot captures
    EA_DIR and friends at import. Restoring HOME before any test body
    runs keeps the mutation off every subprocess this session spawns.
    The scratch dir outlives the context because the tests write into
    EA_DIR / "state".
    """
    global _bot
    if not _ea_bot_present():
        pytest.skip("ea-bot Unix user not present on this host; these tests "
                    "require the isolated runtime account.")
    home = tmp_path_factory.mktemp("ea_bot_home")
    _prepare_home(home)
    with pytest.MonkeyPatch.context() as mp:
        for key, value in _bot_env(home).items():
            mp.setenv(key, value)
        # The bot aborts at import if this is set anywhere in the ambient env.
        mp.delenv("EA_ISOLATION_MODE", raising=False)
        _bot = _import_bot()
    # The dash rewrite writes its body file in the owner-private scratch
    # dir, which the bot resolves when it runs; keep it in the scratch home
    # after the env is put back, off the real ~/.ea-private.
    scratch = home / ".ea-private" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    _bot._private_tmp = lambda: scratch


# --------------------------------------------------------------------------
# _is_emdash_error
# --------------------------------------------------------------------------

def test_is_emdash_error_matches_preflight_message():
    msg = ("Draft contains an em-dash (—) or en-dash (–). User preference: "
           "never use em-dashes in drafted content. Replace with a comma, "
           "period, or rephrase.")
    assert _bot._is_emdash_error(msg) is True


def test_is_emdash_error_ignores_unrelated_errors():
    assert _bot._is_emdash_error("Body contains a Microsoft Teams link.") is False
    assert _bot._is_emdash_error("Subject line is empty.") is False
    assert _bot._is_emdash_error("Reply has no quoted original thread.") is False


# --------------------------------------------------------------------------
# _auto_rewrite_remove_dashes — happy path + failure modes
# --------------------------------------------------------------------------

def _fake_run(call_log, responses):
    """Build a fake subprocess.run that returns scripted results in order.

    `responses` is a list of dicts with keys: stdout, stderr, returncode.
    `call_log` collects each invocation's argv for assertion.
    """
    iter_resp = iter(responses)

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        try:
            r = next(iter_resp)
        except StopIteration as e:
            raise AssertionError(f"unexpected extra subprocess call: {cmd}") from e
        result = MagicMock()
        result.stdout = r.get("stdout", "")
        result.stderr = r.get("stderr", "")
        result.returncode = r.get("returncode", 0)
        return result

    return fake_run


def _get_message_stdout(body: str, body_type: str = "text") -> str:
    """The shape `graph_actions.py get-message` prints.

    cmd_get_message flattens Graph's nested body object into top-level
    `body` (str) and `body_type` (str) keys, and Graph reports
    contentType lowercase. Fixtures that echo Graph's raw
    {"body": {"content", "contentType"}} make the bot read a dict, find
    no dashes in it, and bail before any rewrite runs.
    """
    return json.dumps({"body": body, "body_type": body_type})


EM = "\N{EM DASH}"
EN = "\N{EN DASH}"


def _capture_update(responses, calls, captured):
    """A fake subprocess.run that records each argv, hands back the
    scripted results in order and keeps the body update-draft was given."""
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "update-draft" in cmd:
            path = cmd[cmd.index("--body-file") + 1]
            with open(path, "r", encoding="utf-8") as f:
                captured["text"] = f.read()
        try:
            r = responses[len(calls) - 1]
        except IndexError as e:
            raise AssertionError(f"unexpected extra subprocess call: {cmd}") from e
        result = MagicMock()
        result.stdout = r.get("stdout", "")
        result.stderr = r.get("stderr", "")
        result.returncode = r.get("returncode", 0)
        return result
    return fake_run


def test_auto_rewrite_happy_path_text_body():
    """Fixed rules remove the dash; update-draft is called. No model runs:
    the only subprocess calls are get-message and update-draft."""
    calls, captured = [], {}
    responses = [
        {"stdout": _get_message_stdout(
            f"Hi Edie {EM} see you Tuesday.\n\nBest,\nKit")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_capture_update(responses, calls, captured)), \
            patch.object(_bot, "_remote_text", side_effect=AssertionError("no model call")):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-id-1")
    assert ok is True, f"expected success, got: {status}"
    assert "rewrote" in status.lower() and "fixed rules" in status
    assert len(calls) == 2
    assert calls[0][2:4] == ["get-message", "--message-id"]
    assert calls[1][2:4] == ["update-draft", "--message-id"]
    assert not any(os.path.basename(a) == "claude" for c in calls for a in c), "no Claude call"
    assert captured["text"] == "Hi Edie, see you Tuesday.\n\nBest,\nKit"
    # update-draft uses --body-file pointing into the owner-private scratch
    # dir, never the resident's state dir
    body_file_path = calls[1][calls[1].index("--body-file") + 1]
    assert "/.ea-private/tmp/" in body_file_path and "/state/" not in body_file_path


def test_auto_rewrite_removes_every_dash_form():
    calls, captured = [], {}
    responses = [
        {"stdout": _get_message_stdout(f"Hi {EM} there{EM} and 3{EN}5 pm")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_capture_update(responses, calls, captured)):
        ok, _ = _bot._auto_rewrite_remove_dashes("draft-id-2")
    assert ok is True
    assert EM not in captured["text"] and EN not in captured["text"]
    assert "3 to 5 pm" in captured["text"]


def test_auto_rewrite_aborts_on_clean_body():
    """If the draft has no dashes, nothing is rewritten."""
    calls = []
    responses = [
        {"stdout": _get_message_stdout("Hi Edie, see you Tuesday.")},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-id-3")
    assert ok is False
    assert "no em/en dashes" in status
    # Only the get-message call happened; no update-draft.
    assert len(calls) == 1


def test_auto_rewrite_asks_the_remote_model_only_for_a_dash_the_rules_leave():
    calls, captured, prompts = [], {}, []
    responses = [
        {"stdout": _get_message_stdout(f"Hi {EM} Edie")},
        {"stdout": "{}", "returncode": 0},
    ]

    def remote(prompt, **kw):
        prompts.append((prompt, kw))
        return "```\nHi, Edie\n```"

    original = _bot._replace_dashes
    calls_to_rules = []

    def rules(text):
        # The first pass leaves the dash; the pass over the model's answer
        # is the real one.
        calls_to_rules.append(text)
        return text if len(calls_to_rules) == 1 else original(text)

    with patch("subprocess.run", side_effect=_capture_update(responses, calls, captured)), \
            patch.object(_bot, "_replace_dashes", side_effect=rules), \
            patch.object(_bot, "_remote_text", side_effect=remote):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-id-4")
    assert ok is True, status
    assert "remote model" in status
    assert prompts and prompts[0][1]["purpose"] == "chat-dash-rewrite"
    assert captured["text"] == "Hi, Edie", "fences stripped"


def test_auto_rewrite_keeps_the_fixed_rules_text_without_a_remote_model():
    calls, captured = [], {}
    responses = [
        {"stdout": _get_message_stdout(f"Hi {EM} Edie")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_capture_update(responses, calls, captured)), \
            patch.object(_bot, "_replace_dashes", side_effect=lambda t: t.replace(f" {EM}", ",") + " x -- y"), \
            patch.object(_bot, "_remote_text", return_value=None):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-id-5")
    assert ok is True, status
    assert "fixed rules" in status
    assert captured["text"].startswith("Hi, Edie")


# --- HTML drafts: the path that never worked ------------------------------
# Replies are HTML. get-message without --raw-html replaces the body with
# extracted text truncated to 5000 chars and reports body_type as
# "text (extracted from html)"; .capitalize() then produced
# "Text (extracted from html)", which update-draft's --content-type rejects.
# So the em-dash rewrite aborted on every reply.

def test_auto_rewrite_fetches_raw_html():
    """Without --raw-html the fetched body is truncated extracted text."""
    calls = []
    responses = [
        {"stdout": _get_message_stdout(f"<p>Hi Edie {EM} see you.</p>", "html")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-html-1")
    assert "--raw-html" in calls[0], (
        "get-message must be called with --raw-html or the draft body comes "
        "back as text truncated to 5000 chars")


def test_auto_rewrite_maps_html_content_type_to_a_valid_choice():
    """--content-type must be exactly Text or HTML; 'Html' is rejected."""
    calls = []
    responses = [
        {"stdout": _get_message_stdout(f"<p>Hi {EM} there</p>", "html")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-html-2")
    assert ok is True, f"expected success, got: {status}"
    ct = calls[1][calls[1].index("--content-type") + 1]
    assert ct == "HTML", f"expected 'HTML', got {ct!r}"


def test_auto_rewrite_maps_text_content_type():
    calls = []
    responses = [
        {"stdout": _get_message_stdout(f"Hi {EM} there", "text")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-text-1")
    assert ok is True, f"expected success, got: {status}"
    ct = calls[1][calls[1].index("--content-type") + 1]
    assert ct == "Text", f"expected 'Text', got {ct!r}"


def test_auto_rewrite_refuses_to_overwrite_with_a_truncated_body():
    """A large shrink means the remote model dropped content; the draft
    must survive."""
    long_body = f"<p>Hi Edie {EM} see you Tuesday.</p>" + ("<p>filler line</p>" * 400)
    calls = []
    responses = [
        {"stdout": _get_message_stdout(long_body, "html")},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)), \
            patch.object(_bot, "_replace_dashes", side_effect=lambda t: t), \
            patch.object(_bot, "_remote_text", return_value="<p>Hi Edie, see you Tuesday.</p>"):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-html-3")
    assert ok is False
    assert "lost too much content" in status
    assert len(calls) == 1, "update-draft must not be reached"


def test_auto_rewrite_allows_normal_length_change():
    """Dash substitution shifts length slightly; that must still go through."""
    body = f"Hi Edie {EM} see you Tuesday. " * 40
    calls = []
    responses = [
        {"stdout": _get_message_stdout(body, "text")},
        {"stdout": "{}", "returncode": 0},
    ]
    with patch("subprocess.run", side_effect=_fake_run(calls, responses)):
        ok, status = _bot._auto_rewrite_remove_dashes("draft-text-2")
    assert ok is True, f"expected success, got: {status}"


def test_replace_dashes_plain_text():
    f = _bot._replace_dashes
    assert f(f"Hi Edie {EM} see you Tuesday.") == "Hi Edie, see you Tuesday."
    assert f(f"The plan{EM}which we discussed{EM}is fine.") == \
        "The plan, which we discussed, is fine."
    assert f(f"Oct 3{EN}5 and 10 {EN} 12") == "Oct 3-5 and 10-12"
    assert f(f"Items:\n{EM} one\n{EN} two") == "Items:\n- one\n- two"
    assert f(f"Thanks {EM}\nKit") == "Thanks,\nKit"
    assert f(f"Ready {EM}.") == "Ready."
    assert f("We met -- and agreed.") == "We met, and agreed."
    # The signature delimiter alone on its line stays.
    assert f("Thanks.\n\n-- \nKit") == "Thanks.\n\n-- \nKit"
    for text in (f"Hi Edie {EM} see you", f"a{EN}b", "x -- y"):
        assert not _bot._has_authored_dash(f(text)), text


def test_replace_dashes_html():
    f = _bot._replace_dashes
    assert f(f"<p>Hi Edie {EM} see you.</p>") == "<p>Hi Edie, see you.</p>"
    assert f("<p>Hi &mdash; there</p><p>9&ndash;5</p>") == "<p>Hi, there</p><p>9-5</p>"
    assert f(f"<div>{EM} first</div><br>{EM} second") == "<div>- first</div><br>- second"
    assert f('<a href="x">Edie</a> -- see you') == '<a href="x">Edie</a>, see you'
    assert f("Great seeing you.<br>-- <br>Kit") == "Great seeing you.<br>-- <br>Kit"
    assert f(f"<p>Done {EM}</p>") == "<p>Done,</p>"
    for text in (f"<p>Hi {EM} there</p>", "<b>Edie</b> -- see you"):
        assert not _bot._has_authored_dash(f(text)), text


def test_replace_dashes_leaves_tags_urls_and_comments():
    f = _bot._replace_dashes
    link = '<a href="https://docs.example.com/d/abc--def/edit">doc</a>'
    assert f(link + " -- see you") == link + ", see you"
    img = '<img src="https://cdn.example.com/a--b.png" alt="x">'
    assert f(img) == img
    for text in ("See https://docs.example.com/d/abc--def/edit for it.",
                 "Our site is xn--bcher-kva.example now.",
                 "<!-- EA_REPLY_START --><p>Hi</p><!-- EA_REPLY_END -->",
                 "Step A --> step B.",
                 "Write to a--b@example.org today.",
                 f'<p title="a {EM} b" style="font:a-b, c">Hi</p>'):
        assert f(text) == text, text
        assert not _bot._has_authored_dash(text), text
    assert f(f'<div class="x-y">Hi {EM} there</div>') == '<div class="x-y">Hi, there</div>'


def test_replace_dashes_en_dash_ranges_and_compounds():
    f = _bot._replace_dashes
    assert f(f"Free 9:30 AM{EN}10:00 AM on Tuesday.") == "Free 9:30 AM to 10:00 AM on Tuesday."
    assert f(f"Free 9:30{EN}10:00 or 2{EN}3pm.") == "Free 9:30 to 10:00 or 2 to 3pm."
    assert f(f"The Boulder{EN}Denver trip.") == "The Boulder-Denver trip."
    assert f(f"Pages 3{EN}5.") == "Pages 3-5."
    # A spaced dash between words is still prose punctuation.
    assert f(f"Hi Edie {EN} see you.") == "Hi Edie, see you."


def test_dash_detector_ignores_the_outlook_signature_delimiter():
    """Outlook writes "--" as the signature delimiter on every reply, so a
    substring test would fire on every draft and hand the whole body to a
    rewriter that eats the signature."""
    for body in (
        "Great seeing you.<br>-- <br>Kit Calloway",
        "Thanks.\n\n-- \nKit Calloway\nAssociate Professor",
        "A clean body with no dashes at all.",
    ):
        assert _bot._has_authored_dash(body) is False, body


def test_dash_detector_catches_ascii_double_hyphen_as_punctuation():
    """CLAUDE.md names "--" explicitly, so a gate that looks only for the
    true em/en dash lets an ASCII double hyphen through."""
    for body in (
        "send thoughts -- genuinely interested in collaboration",
        "<div>Ship it -- then tell me</div>",
        "word--word joined",
        "This has a real em-dash — right here",
        "An en-dash – also caught",
    ):
        assert _bot._has_authored_dash(body) is True, body



# --------------------------------------------------------------------------
# every handler is in the registry; the bot binds nothing to bolt
# --------------------------------------------------------------------------

ACTIONS = {"approve_send", "cancel_send", "approve_schedule_send",
           "cancel_schedule_send", "sent_myself", "show_quoted", "hide_quoted",
           "approve_event", "cancel_event", "approve_event_update",
           "cancel_event_update", "approve_event_delete", "cancel_event_delete",
           "plugin_card_approve", "plugin_card_cancel", "approve_publish", "cancel_publish",
           "approve_bulk_send", "dismiss_bulk_send", "edit_draft", "draft_handled",
           "home_refresh", "home_link_3", "home_ask_item", "home_ask", "home_ask_custom",
           "home_file", "home_card_menu", "home_item_done",
           "room_open_project", "room_pick_project", "room_create_project",
           "close_all_proposed", "review_proposed", "close_proposed",
           "keep_open_proposed", "reopen_closed", "approve_browser", "cancel_browser"}
VIEWS = {"edit_draft_modal", "home_ask_modal", "home_journal_modal"}


def test_every_handler_is_in_the_registry_and_none_on_bolt():
    """Every action, view and event handler takes a neutral event and is
    recorded in the registry; the frontend's own listeners (registered by
    FRONTEND.start(), which the import does not run) dispatch them, so the
    bot makes no call on bolt at all."""
    bolt = _bot.FRONTEND.bolt
    assert bolt.mock_calls == [], bolt.mock_calls
    registry = _bot.FRONTEND.registry
    assert _bot.app is registry
    for key in ACTIONS:
        assert registry._find(key, registry._actions, registry._action_patterns), key
    for key in VIEWS:
        assert registry._find(key, registry._views, registry._view_patterns), key
    src = (PROJECT_ROOT / "ea-chat-bot.py").read_text()
    assert src.count("\n" + "@app.action(") == len(ACTIONS)
    assert src.count("\n" + "@app.view(") == len(VIEWS)
    assert registry._message_handlers == [_bot.on_message]
    for key in ("home_opened", "queue_reset"):
        assert registry._find(key, registry._events, registry._event_patterns), key
    assert registry._find("anything_else", registry._events,
                          registry._event_patterns) is _bot.catch_all_events


if __name__ == "__main__":
    if not _ea_bot_present():
        print("SKIP: ea-bot Unix user not present on this host; test requires "
              "the isolated runtime account.")
        sys.exit(0)
    # This process is throwaway, so mutate os.environ outright. Under
    # pytest the same setup goes through the _bot_module fixture.
    _home = Path(tempfile.mkdtemp(prefix="ea_bot_test_home_"))
    _prepare_home(_home)
    os.environ.update(_bot_env(_home))
    os.environ.pop("EA_ISOLATION_MODE", None)
    _bot = _import_bot()

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


