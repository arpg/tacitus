#!/usr/bin/env python3
"""Regression: a reply whose source email was archived/moved must still thread.

Archiving changes an Outlook message's id, so a ledger row holding the
inbox id makes `create-reply` POST to a dead id and take a 404. Falling
back to a fresh `create-draft` produces a reply with NO quoted thread.

In graph_client.create_draft: when a reply 404s and a conversation_id is
known, re-resolve the live message in that conversation and retry, so
the quoted thread is preserved. These tests pin that recovery and the
fail-closed behavior (no conversation_id -> raise, do NOT silently drop
the thread).

The other half is the caller who never asked for a reply. create-reply
refuses to draft when the user sent the latest message in the
conversation, so a create-draft with a reply-shaped subject of its own is
a route around that guard, and create-draft refuses those subjects.
"""

import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

import requests

# Never touch production state (graph_client and graph_actions both bind
# EA_DIR at import).
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-reply-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import graph_actions as ga  # noqa: E402
import graph_client as gc  # noqa: E402


def _http_404():
    resp = types.SimpleNamespace(status_code=404)
    return requests.exceptions.HTTPError("404 Not Found", response=resp)


class _Patched:
    """Monkeypatch graph_client's module-level Graph helpers for one test."""

    def __init__(self, post=None, get=None, patch=None):
        self._new = {"graph_post": post, "graph_get": get, "graph_patch": patch}
        self._old = {}

    def __enter__(self):
        for name, fn in self._new.items():
            if fn is not None:
                self._old[name] = getattr(gc, name)
                setattr(gc, name, fn)
        return self

    def __exit__(self, *exc):
        for name, fn in self._old.items():
            setattr(gc, name, fn)


CONV = "CONV-ABC"
_CONV_VALUE = [
    {"id": "DRAFT_X", "isDraft": True, "receivedDateTime": "2026-06-07T00:00:00Z"},
    {"id": "LIVE_LATEST", "isDraft": False, "receivedDateTime": "2026-06-06T23:33:30Z"},
    {"id": "OLDER", "isDraft": False, "receivedDateTime": "2026-06-05T20:00:00Z"},
]


def test_stale_reply_reresolves_and_retries():
    posts = []

    def fake_post(endpoint, body):
        posts.append(endpoint)
        if "STALE" in endpoint:
            raise _http_404()
        # the retry against the live id succeeds with a quoted thread
        return {"id": "NEWDRAFT", "body": {"content": "<div>quoted thread</div>"}}

    def fake_get(endpoint, params=None):
        assert endpoint == "/me/messages"
        assert CONV in params["$filter"]
        return {"value": _CONV_VALUE}

    with _Patched(post=fake_post, get=fake_get, patch=lambda ep, b: {"id": "NEWDRAFT"}):
        result = gc.create_draft(
            to=[], subject="", body="Hi Kai, here are some thoughts.",
            reply_to_id="STALE_ID", content_type="Text", conversation_id=CONV,
        )

    assert result["id"] == "NEWDRAFT", result
    # Two POSTs: the stale id (404) then the re-resolved live id.
    assert len(posts) == 2, posts
    assert "STALE" in posts[0]
    assert "LIVE_LATEST" in posts[1], posts


def test_resolver_picks_latest_non_draft():
    with _Patched(get=lambda ep, params=None: {"value": _CONV_VALUE}):
        assert gc._resolve_latest_in_conversation(CONV) == "LIVE_LATEST"


def _msg(mid, sender, when):
    return {"id": mid, "isDraft": False, "receivedDateTime": when,
            "from": {"emailAddress": {"address": sender}}}


def test_a_branched_thread_does_not_re_resolve_a_reply():
    # The user forwarded the request to a colleague, who answered: the
    # newest message is an aside, and a reply to it would go to the
    # colleague and quote the aside.
    branched = {"value": [
        _msg("REQ", "asker@example.edu", "2026-01-01T00:00:00Z"),
        _msg("FWD", "owner@example.edu", "2026-01-01T00:05:00Z"),
        _msg("ASIDE", "colleague@example.edu", "2026-01-01T00:07:00Z")]}
    with _Patched(get=lambda ep, params=None: branched):
        assert gc._resolve_latest_in_conversation(CONV) == "ASIDE"   # snooze wake
        try:
            gc._resolve_latest_in_conversation(CONV, one_correspondent=True)
        except gc.AmbiguousReplyTarget as e:
            assert "colleague@example.edu" in str(e) and "--message-id" in str(e)
        else:
            raise AssertionError("a branched thread must not re-resolve a reply")


def test_a_two_person_thread_still_re_resolves():
    pair = {"value": [_msg("A", "asker@example.edu", "2026-01-01T00:00:00Z"),
                      _msg("B", "owner@example.edu", "2026-01-01T00:05:00Z")]}
    with _Patched(get=lambda ep, params=None: pair):
        assert gc._resolve_latest_in_conversation(CONV, one_correspondent=True) == "B"


def test_resolver_none_when_only_drafts():
    only_drafts = {"value": [{"id": "D", "isDraft": True, "receivedDateTime": "x"}]}
    with _Patched(get=lambda ep, params=None: only_drafts):
        assert gc._resolve_latest_in_conversation(CONV) is None


def test_404_without_conversation_id_raises():
    """No conversation_id -> cannot recover -> propagate, never silently
    produce a quote-less draft."""
    def fake_post(endpoint, body):
        raise _http_404()

    with _Patched(post=fake_post):
        try:
            gc.create_draft(to=[], subject="", body="hi",
                            reply_to_id="STALE_ID")  # no conversation_id
        except requests.exceptions.HTTPError:
            return
    raise AssertionError("expected HTTPError to propagate without conversation_id")


def test_404_unresolvable_conversation_raises():
    """conversation_id given but nothing resolvable -> raise, don't fall back."""
    def fake_post(endpoint, body):
        raise _http_404()

    with _Patched(post=fake_post, get=lambda ep, params=None: {"value": []}):
        try:
            gc.create_draft(to=[], subject="", body="hi",
                            reply_to_id="STALE_ID", conversation_id=CONV)
        except requests.exceptions.HTTPError:
            return
    raise AssertionError("expected HTTPError when conversation has no live message")


# ── create-draft must not open a reply (the Pathway re-draft) ────────────

def _run_create_draft(subject: str, **extra):
    """Drive cmd_create_draft and return (parsed stdout, drafts created)."""
    import io
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    created = []

    def fake_create_draft(**kwargs):
        created.append(kwargs)
        return {"id": "NEWDRAFT", "conversationId": "CONV-NEW"}

    args = SimpleNamespace(
        to="ash.calloway87@example.edu", cc=None, subject=subject,
        body="Hi Ira, yes to all three.", content_type="Text",
        force_new_thread=extra.get("force_new_thread", False))
    buf = io.StringIO()
    with patch.object(ga.graph, "create_draft", fake_create_draft), \
            patch.object(ga._draft_events, "record", lambda *a, **k: None), \
            patch.object(ga._draft_capture, "capture", lambda *a, **k: None), \
            patch("sys.stdout", buf):
        ga.cmd_create_draft(args)
    return json.loads(buf.getvalue()), created


def test_create_draft_refuses_a_reply_shaped_subject():
    """The exact Pathway subject. No draft may reach Graph."""
    out, created = _run_create_draft(
        "Re: Pathway Bridge to the Fellowship Gala - RSVP")
    assert out["status"] == "refused", out
    assert created == [], "a threadless re-draft was created anyway"
    # The refusal has to hand back the recovery path, or the caller just
    # rewords the subject and tries again.
    instead = " ".join(out["do_this_instead"])
    assert "create-reply" in instead
    assert "get-conversation" in instead
    assert "NO --folder" in instead


def test_create_draft_refuses_every_reply_and_forward_prefix():
    for subject in ("RE: x", "re: x", "Re : x", "  Re: x", "FW: x",
                    "Fwd: x", "RE[2]: x", "AW: x"):
        out, created = _run_create_draft(subject)
        assert out["status"] == "refused", f"{subject!r} slipped through"
        assert created == [], subject


def test_create_draft_still_opens_a_genuinely_new_thread():
    """The guard keys on the prefix, not on the word 'reply'. A normal new
    subject, and a subject that merely CONTAINS 're:', must both pass."""
    for subject in ("Pathway gala", "Reappointment docs",
                    "Question re: the gala"):
        out, created = _run_create_draft(subject)
        assert out["status"] == "draft_created", (subject, out)
        assert len(created) == 1, subject


def test_force_new_thread_is_the_documented_escape():
    out, created = _run_create_draft("Re: brand new thread",
                                     force_new_thread=True)
    assert out["status"] == "draft_created", out
    assert len(created) == 1


def test_force_new_thread_flag_exists_on_the_cli():
    """The refusal names an escape; argparse has to actually offer it, or a
    caller with a legitimate Re: subject is stuck."""
    src = (HERE.parent / "preprocessor" / "graph_actions.py").read_text()
    block = src[src.index('sub.add_parser("create-draft"'):]
    block = block[:block.index("set_defaults")]
    assert "--force-new-thread" in block, block


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
