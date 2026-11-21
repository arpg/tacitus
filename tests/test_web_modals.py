"""A window (modal) a card button opens on the web chat: the click mints a
trigger, the core's open_modal holds the view for it, the app fetches it
and saves the inputs back as a ViewEvent (docs/webchat.md, Windows)."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))

from chat import ActionEvent, CardRegistry, MessageRef, ViewEvent  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.shadow_frontend import ShadowFrontend  # noqa: E402
from chat.types import WEB_TRIGGER  # noqa: E402
from test_web_frontend import card, make, owner  # noqa: E402

VIEW = {
    "type": "modal", "callback_id": "edit_draft_modal",
    "private_metadata": json.dumps({"draft_id": "d-1"}),
    "title": {"type": "plain_text", "text": "Edit draft"},
    "submit": {"type": "plain_text", "text": "Save"},
    "blocks": [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "*To:* a@example.test"}]},
        {"type": "input", "block_id": "body_block", "label": {"type": "plain_text", "text": "Body"},
         "element": {"type": "plain_text_input", "action_id": "body", "multiline": True,
                     "initial_value": "Hello"}},
        {"type": "input", "block_id": "kind", "optional": True,
         "label": {"type": "plain_text", "text": "Kind"},
         "element": {"type": "static_select", "action_id": "value",
                     "options": [{"text": {"type": "plain_text", "text": k}, "value": k}
                                 for k in ("note", "decision")]}},
    ],
}


@pytest.fixture
def fe(tmp_path):
    f = make(tmp_path)
    f.start()
    yield f
    f.stop()


def _wire(f, open_view=True):
    """An Edit handler that opens VIEW, and a submit handler that records."""
    saved, done = [], threading.Event()

    @f.registry.action("approve_send")
    def edit(ev):
        if open_view:
            f.open_modal(ev.trigger_id, VIEW)

    @f.registry.view("edit_draft_modal")
    def submit(ev):
        saved.append(ev)
        done.set()
    return saved, done


def _click(f):
    ref = f.post_card("dm", card())
    status, _h, body = owner(f, "POST", f"/api/cards/dm/{ref.id}/actions",
                             {"action_id": "approve_send"})
    assert status == 200
    return json.loads(body)["trigger"]


def test_a_click_that_opens_a_window_shows_it_and_saves_it_as_a_view_event(fe):
    saved, done = _wire(fe)
    trigger = _click(fe)
    assert trigger.startswith(WEB_TRIGGER)
    status, _h, body = owner(fe, "GET", f"/api/modals/{trigger}")
    view = json.loads(body)["modal"]
    assert status == 200 and view["title"]["text"] == "Edit draft"
    assert "private_metadata" not in view and "callback_id" not in view

    status, _h, body = owner(fe, "POST", f"/api/modals/{trigger}", {"values": {
        "body_block": {"body": "Hello again"}, "kind": {"value": "decision"},
        "forged": {"x": "y"}}})
    assert status == 200, body
    assert done.wait(5)
    ev = saved[0]
    assert isinstance(ev, ViewEvent)
    assert (ev.callback_id, ev.user_id) == ("edit_draft_modal", "owner")
    assert json.loads(ev.private_metadata) == {"draft_id": "d-1"}   # the stored view's
    assert ev.values == {"body_block": {"body": {"type": "plain_text_input", "value": "Hello again"}},
                         "kind": {"value": {"type": "static_select",
                                            "selected_option": {"value": "decision"}}}}
    # One save per click.
    assert owner(fe, "POST", f"/api/modals/{trigger}",
                 {"values": {"body_block": {"body": "x"}}})[0] == 404


def test_a_click_that_opens_no_window_answers_at_once(fe):
    _wire(fe, open_view=False)
    trigger = _click(fe)
    status, _h, body = owner(fe, "GET", f"/api/modals/{trigger}")
    assert status == 200 and json.loads(body) == {"modal": None}


def test_the_saved_values_are_checked_against_the_view(fe):
    _wire(fe)
    trigger = _click(fe)
    owner(fe, "GET", f"/api/modals/{trigger}")
    post = lambda v: owner(fe, "POST", f"/api/modals/{trigger}", {"values": v})[0]  # noqa: E731
    assert post({"body_block": {"body": "  "}}) == 400                       # required
    assert post({"body_block": {"body": "x" * 3001}}) == 400                 # too long
    assert post({"body_block": {"body": "ok"}, "kind": {"value": "nope"}}) == 400
    assert post({"body_block": {"body": "ok"}}) == 200                       # still open after a refusal


def test_only_a_trigger_minted_here_takes_a_view(fe):
    assert fe.open_modal("web:never-minted", VIEW) is False
    assert fe.open_modal("", VIEW) is False
    assert owner(fe, "GET", "/api/modals/web:never-minted")[0] == 404
    assert owner(fe, "POST", "/api/modals/web:never-minted", {"values": {}})[0] == 404


def test_shadow_mode_opens_a_web_clicks_window_on_the_web_and_saves_it_as_the_owner(tmp_path):
    primary = MemoryFrontend(CardRegistry(tmp_path / "cards.json"))
    web = make(tmp_path)
    fe = ShadowFrontend(primary, web)
    fe.start()
    try:
        saved = []

        @fe.registry.action("approve_send")
        def edit(ev):
            fe.open_modal(ev.trigger_id, VIEW)

        @fe.registry.view("edit_draft_modal")
        def submit(ev):
            saved.append(ev)

        cref = fe.post_card("D_OWNER", card())
        fe.card_registry.add(cref, card(), code=None)
        trigger = web.new_modal_trigger()
        ev = ActionEvent(action_id="approve_send", user_id="owner", trigger_id=trigger,
                         card=MessageRef(cref.channel, cref.id, cref.thread_key))
        assert web.inbound("action", event=ev) is True
        assert primary.modals == []                       # never on the primary
        assert web.modal_view(trigger, wait_s=0)["title"]["text"] == "Edit draft"
        web.modal_submit(trigger, {"body_block": {"body": "Edited"}})
        assert saved[0].user_id == "U_OWNER"
        assert saved[0].values["body_block"]["body"]["value"] == "Edited"

        # A Slack click still opens its window on Slack.
        fe.open_modal("1234.slack-trigger", VIEW)
        assert primary.modals[-1]["trigger_id"] == "1234.slack-trigger"
    finally:
        fe.stop()
