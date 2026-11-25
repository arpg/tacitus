"""The app's six verbs, Send, Draft, Done, Later, Not done and Ask, and
two exceptions, Edit (change a draft that exists) and Tell Tacitus (the
one note on a Home item, a question, a direction or both, which the owner
named himself: Ask read wrong for a direction): every button label
the code can draw for a done, not-done, later, ask, draft or edit act is
one of their words, so the words do not drift again. Try again is only
the retry of something that failed.

The labels are read from the code itself: every CardAction the bot builds
(ea-chat-bot.py), every Home choice and step (home_items.py,
home_deeper.py, case_surface.py, case_briefing.py), each work kind's own
words (case_surface.Words) and the web app's own buttons (app.js). Each
action and choice id has a meaning here; an id with none fails, so a new
button gets its verb before it ships. A count ("Done (3)") or an object
("Done: I sent it", "Later: Tue Dec 1", "Submitted: North Campus") may
follow a verb; a second word for the same act may not."""
from __future__ import annotations

import ast
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))
sys.path.insert(2, str(ROOT / "botcore"))

import case_surface as C  # noqa: E402
import home_deeper as HD  # noqa: E402

# What each act is. "send" and "cancel" are the gated family, behind the
# lock; "answer" is a question's own words (Yes, No, the offered answers,
# a date); "pick" chooses among places (a room, a part); "go" opens a
# place and does nothing there; "split" posts one card per item of a
# batch; "recard" brings a draft's Send card back; "retry" starts again a
# draft that did not start; "decline" says No to a
# work request after a Yes; "hold" puts a block on the calendar; "retry"
# reads a briefing again.
CARD = {
    "approve_send": "send", "approve_schedule_send": "send", "approve_bulk_send": "send",
    "approve_event": "send", "approve_event_update": "send", "approve_event_delete": "send",
    "approve_publish": "send", "plugin_card_approve": "send", "approve_browser": "send",
    "cancel_send": "cancel", "cancel_schedule_send": "cancel", "dismiss_bulk_send": "cancel",
    "cancel_event": "cancel", "cancel_event_update": "cancel", "cancel_event_delete": "cancel",
    "cancel_publish": "cancel", "plugin_card_cancel": "cancel", "cancel_browser": "cancel",
    "edit_draft": "edit", "reply_draft": "draft",
    "draft_handled": "done", "sent_myself": "done", "close_proposed": "done",
    "close_all_proposed": "done", "reply_not_needed": "done",
    "keep_open_proposed": "not_done", "reopen_closed": "not_done",
    "reply_skip": "later",
    "reply_discuss": "ask",
    "reply_choice_{}": "answer",
    "room_open_project": "pick", "room_pick_project": "pick", "room_create_project": "pick",
    "review_proposed": "split", "review_replies": "split",
}
# One id, more than one act, told apart by the label: Draft's id also
# carries a question's typed answer, a draft's lost Send card and the retry
# of a draft that did not start.
CARD_BY_LABEL = {("reply_draft", "Other answer"): "answer", ("reply_draft", "New Send card"): "recard",
                 ("reply_draft", "Try again"): "retry"}
HOME = {
    "draft": "draft", "say": "draft", "not_needed": "done", "done": "done", "start": "ask",
    "waiting": "later", "later": "later", "reopen": "not_done", "open": "go", "outlook": "go",
    "answer": "answer", "answer:{}": "answer",
    # one list: a close proposal's Not done, a "which one was it?" pick, and
    # the note box's one button (the owner's words to Tacitus)
    "not_done": "not_done", "which:{}": "answer", "note": "tell",
}
# A work case's choices and buttons, by the answer after the kind's prefix.
CASE = {
    "yes": "answer", "yes_ask": "answer", "yes_date": "answer", "no": "answer", "date": "answer",
    "date_pick": "answer", "draft": "draft_or_edit", "all": "done", "decline": "decline",
    "delivered": "done", "submitted": "done", "returned": "done",
    # "Is this the same person?", asked first on Home with one list
    "join_same": "answer", "join_other": "answer",
}
# One name per item type on Home (docs/webchat.md, The six verbs): each
# kind's label, and the kinds one list adds.
ITEM_NAMES = {"reply": "Reply owed", "question": "Answer needed", "proposal": "Looks done",
              "which": "Answer needed", "conversation": "Answer needed"}
# The words each act may use. A Send-family label names its own act.
ALLOWED = {
    "done": r"Done|Done \(\{\}\)|Done: .+|No reply needed|(Submitted|Returned|Delivered)(: .+)?",
    "not_done": r"Not done",
    "later": r"Later|Later: .+",
    "ask": r"Ask|Ask: .+",
    "draft": r"Draft|Draft with my words|Draft: .+|Draft (the|a) .+",
    "edit": r"Edit|Edit the .+",
    # the one note on a Home item: a question, a direction, or both
    "tell": r"Tell Tacitus",
    # a work case's one draft button: Draft before a draft exists, Edit after
    "draft_or_edit": r"Draft|Draft (the|a) .+|Edit|Edit the .+",
    "retry": r"Try again",
    "cancel": r"Cancel|\{\}",
    "send": r"Send|Send all \{\}|Schedule|Create|Reschedule|Delete|Publish|Push|\{\}",
}
# Labels the app used for these acts before the six verbs; none may come back.
RETIRED = {
    "Mark done", "Mark it done", "Mark all done", "Close", "Close all", "Close all {}", "Handled",
    "Sent myself", "Not needed", "All submitted", "Still open", "Reopen", "Not done yet",
    "Needs me", "Skip 3 days", "Waiting on someone", "Open in Ask Tacitus", "Ask about this",
    "Ask about it", "Answer it", "Start it with Tacitus", "Continue in a conversation",
    "Draft it", "Draft a reply", "Draft the reply", "Tell Tacitus what to say", "Draft again",
    "Keep", "Fill it in", "Change the letter", "Change the report", "Change the answers",
    "Change the draft",
}
EMOJI = re.compile(r"^\s*(?::[a-z0-9_+-]+:\s*)+")


def _words(label: str) -> str:
    return EMOJI.sub("", label).strip()


class _Labels(ast.NodeVisitor):
    """Every label a source file can give a CardAction or a choice dict,
    with its id; an f-string's parts as "{}"."""

    def __init__(self, tree: ast.Module):
        self.consts: dict[str, list[str]] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.consts[target.id] = self.texts(node.value)
                    elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                        for t, v in zip(target.elts, node.value.elts):
                            if isinstance(t, ast.Name):
                                self.consts[t.id] = self.texts(v)
        self.found: list[tuple[str, str]] = []

    def texts(self, node) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            out = [""]
            for part in node.values:
                if isinstance(part, ast.Constant):
                    out = [o + str(part.value) for o in out]
                else:
                    out = [o + "{}" for o in out]
            return out
        if isinstance(node, ast.IfExp):
            return self.texts(node.body) + self.texts(node.orelse)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return [a + b for a in self.texts(node.left) for b in self.texts(node.right)]
        if isinstance(node, ast.Name):
            return self.consts.get(node.id, ["{}"])
        return ["{}"]

    def visit_Call(self, node):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "CardAction" and len(node.args) >= 2 and not isinstance(node.args[0], ast.Subscript):
            for aid in self.texts(node.args[0]):
                for label in self.texts(node.args[1]):
                    self.found.append((aid, label))
        self.generic_visit(node)

    def visit_Dict(self, node):
        keys = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
        if "id" in keys and "label" in keys:
            for aid in self.texts(keys["id"]):
                for label in self.texts(keys["label"]):
                    self.found.append((aid, label))
        self.generic_visit(node)


def _scan(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    v = _Labels(tree)
    v.visit(tree)
    return v.found


def _check(meaning: str, label: str) -> bool:
    return bool(re.fullmatch(ALLOWED[meaning], _words(label))) if meaning in ALLOWED else True


def _case_meaning(aid: str) -> str | None:
    """A work case's id, "<prefix>:<answer>[:<part>]" on Home or
    "<prefix>_<answer>" on a card, as its act."""
    m = re.fullmatch(r"(?:\{\}|letter|case)[:_]([a-z_]+?)(?::[^:]+)?", aid)
    return CASE.get(m.group(1)) if m else None


def test_every_bot_card_label_is_in_its_verbs_words():
    bad, unknown = [], []
    for aid, label in _scan(ROOT / "ea-chat-bot.py"):
        if aid == "{}":
            continue  # a case's own button, from its spec (below)
        meaning = CARD_BY_LABEL.get((aid, _words(label))) or CARD.get(aid)
        if meaning is None:
            unknown.append(aid)
        elif not _check(meaning, label):
            bad.append((aid, label))
    assert not unknown, f"give these actions a meaning in CARD: {sorted(set(unknown))}"
    assert not bad, f"labels outside the six verbs: {bad}"


def test_every_home_choice_label_is_in_its_verbs_words():
    bad, unknown = [], []
    for path in ("preprocessor/home_items.py", "preprocessor/case_surface.py"):
        for aid, label in _scan(ROOT / path):
            if aid in ("{}:{}", "{}:{}:{}") or _words(label).startswith("{}"):
                continue  # a kind's own words: built and checked below
            meaning = HOME.get(aid) or _case_meaning(aid)
            if meaning is None:
                unknown.append((path, aid))
            elif not _check(meaning, label):
                bad.append((aid, label))
    assert not unknown, f"give these choices a meaning in HOME or CASE: {unknown}"
    assert not bad, f"labels outside the six verbs: {bad}"


def test_the_deeper_briefing_steps_speak_the_verbs():
    p = {"say": "Yes to Tuesday", "question": "Which room?", "ask": "the budget table",
         "date": "2026-12-01", "minutes": 30}
    kinds = {"draft_reply": "draft", "ask_sender": "draft", "propose_times": "draft",
             "wait_until": "later", "mark_done": "done", "work_on_it": "ask"}
    for kind, meaning in kinds.items():
        assert _check(meaning, HD._step_label(kind, p)), (kind, HD._step_label(kind, p))
    assert HD._step_label("mark_done", p, "not_needed") == "No reply needed"
    assert HD.CONTINUE_LABEL == "Ask"
    for kind, head in HD.STEP_HEADS.items():
        assert _check(kinds[kind], head), (kind, head)


def test_each_work_kind_says_its_verbs():
    for w in C.WORDS.values():
        assert _check("draft", w.draft) and _check("edit", w.change), (w.kind, w.draft, w.change)
        assert _check("done", w.delivered_button.format(name="North Campus")), w.delivered_button
        assert _check("done", w.delivered_another), w.delivered_another
    labels = {_words(label) for _aid, label in _scan(ROOT / "preprocessor/case_briefing.py")}
    assert not labels & RETIRED


def _part(pid, name, state):
    return {"id": pid, "case": "", "name": name, "due": "2026-12-01", "method": "email", "link": "",
            "state": state, "ledger_id": f"row-{pid}", "conversation_id": f"conv-{pid}", "sender": "",
            "draft": "", "delivered_at": "", "evidence": ""}


def _case(kind, state):
    return {"id": f"{kind}-1", "kind": kind, "subkind": "", "counterpart_name": "Pat Lee",
            "counterpart_email": "pat.lee@example.org", "title": "", "status": "open",
            "decision": "accepted", "opened": "2026-11-10", "closed_at": "", "materials": [],
            "base_doc": "", "notes": "", "keep_level": "full", "case_ledger_id": "",
            "parts": [_part("p1", "North Campus", state), _part("p2", "South Campus", state)],
            "questions": [], "case_kind": kind, "do_by": "2026-12-01"}


def test_a_case_card_and_its_home_item_speak_the_verbs(monkeypatch):
    """The block (Draft, the date, Decline, a part already done) and the
    ready list (one Done per part, all of them, Edit), on the card
    and on Home, for every kind."""
    monkeypatch.setattr(C, "_cases", lambda: None)
    today = date(2026, 11, 28)
    for kind in ("review", "form", "report"):
        for state in ("accepted", "ready"):
            case = _case(kind, state)
            spec = C.case_spec(case, today)
            labels = [(aid, label) for aid, label, *_rest in spec["actions"]]
            if spec.get("select"):
                labels.append((spec["select"][0], spec["select"][1]))
            labels += [(c["id"], c["label"]) for c in C.home_choices(case, today)]
            assert labels
            for aid, label in labels:
                meaning = HOME.get(aid) or _case_meaning(aid)
                assert meaning and _check(meaning, label), (kind, state, aid, label)
                assert _words(label) not in RETIRED, (kind, state, label)


def test_no_retired_label_comes_back():
    found = []
    for path in ("ea-chat-bot.py", "preprocessor/home_items.py", "preprocessor/case_surface.py",
                 "preprocessor/home_deeper.py", "preprocessor/case_briefing.py"):
        found += [(path, label) for _aid, label in _scan(ROOT / path) if _words(label) in RETIRED]
    js = (ROOT / "botcore/botcore/chat/webui/app.js").read_text(encoding="utf-8")
    html = (ROOT / "botcore/botcore/chat/webui/index.html").read_text(encoding="utf-8")
    for label in RETIRED - {"Close", "Keep"}:
        if re.search(r"""['"`]%s['"`]""" % re.escape(label), js):
            found.append(("app.js", label))
        if re.search(r">%s<" % re.escape(label), html):
            found.append(("index.html", label))
    assert not found, f"retired labels: {found}"


def test_the_web_app_thread_buttons_are_done_and_not_done():
    js = (ROOT / "botcore/botcore/chat/webui/app.js").read_text(encoding="utf-8")
    assert "=== 'done' ? 'Not done' : 'Done'" in js
    assert "`Done (${rows.length})`" in js


def test_typed_verbs_reach_the_older_words():
    from botcore.chat import cards
    items = [("close_proposed", "primary", "close"), ("keep_open_proposed", "danger", "keep")]
    assert cards._word_action(items, "done") == "close_proposed"
    assert cards._word_action(items, "not done") == "keep_open_proposed"
    assert cards._word_action(items, "close") == "close_proposed"
    assert cards._word_action(items, "keep") == "keep_open_proposed"
    send = [("approve_send", "primary", "send"), ("cancel_send", "danger", "cancel"),
            ("edit_draft", "", "edit"), ("draft_handled", "", "handled"), ("sent_myself", "", "sent")]
    assert cards._word_action(send, "no reply needed") == "draft_handled"
    assert cards._word_action(send, "edit") == "edit_draft"
    retry = [("reply_draft", "primary", "again"), ("reply_discuss", "", "ask")]
    assert cards._word_action(retry, "try again") == "reply_draft"
    assert cards._word_action(retry, "again") == "reply_draft"
    assert cards._word_action(send, "handled") == "draft_handled"
    assert cards._word_action(send, "done") is None  # Done: I sent it is never a guess
    delete = [("approve_event_delete", "danger", "delete"), ("cancel_event_delete", "", "keep")]
    assert cards._word_action(delete, "cancel") == "cancel_event_delete"


def test_the_closure_labels_read_the_new_lines_as_they_read_the_old():
    """closure_labels.outcome_class turns a card's status line into the
    outcome the closure analysis counts: each new line lands where its old
    line did."""
    import closure_labels as cl
    pairs = [
        (":pushpin: Kept open. I will not suggest closing it on the same evidence again.",
         ":pushpin: Not done. It stays on your list."),
        (":information_source: Already closed or changed.",
         ":information_source: It changed since the card. Nothing was done."),
        (":incoming_envelope: Marked as sent by user.", ":incoming_envelope: Done. You sent it yourself."),
        (":wastebasket: Every draft on this card was already handled.",
         ":information_source: Every draft on this card has its answer. Nothing was done here."),
        (":ok_hand: One by one it is.", ":no_entry_sign: Cancelled. Each draft keeps its own card."),
        (":x: Send failed: the server said no", ":x: Not sent: the server said no. Ask me to try again."),
        (":x: This batch has expired. Use the buttons on each draft.",
         ":x: Nothing was sent: this card is too old. Use the card of each draft."),
        (":white_check_mark: Closed.", ":white_check_mark: Done. It is off your list."),
    ]
    for old, new in pairs:
        assert cl.outcome_class(new) == cl.outcome_class(old), (old, new)


def test_each_home_item_kind_has_its_one_name():
    """The item types Home lists, the ones one list adds among them (a
    close proposal, a "which one was it?" question, a conversation that
    needs the owner), each under its one name; their choices are in HOME."""
    sys.path.insert(0, str(ROOT / "tests"))
    import home_fixtures as F
    import home_items as I
    import home_screen as H
    src = F.sources()
    src.one_list = True
    src.rows["task-s2"]["close_proposal"] = {"why": "The slides went out", "first_at": "2026-11-20T10:00:00Z"}
    src.replies[1]["queued"] = {"outcome": "decision", "note": "Sign it?", "choices": ["Yes", "No"]}
    src.questions = [{"id": "q1", "kind": "part", "text": "Which one went in?", "choices": ["One", "None of these"]}]
    src.cases = []
    src.conversations = [{"channel": "main", "thread_key": "t1", "text": "Which room?", "state": "needs_you",
                          "state_reason": "A question waits for you.", "_ids": [], "_convs": []}]
    seen = set()
    for x in H.left(src):
        if x["kind"] in ITEM_NAMES:
            seen.add(x["kind"])
            assert x["label"] == ITEM_NAMES[x["kind"]], (x["kind"], x["label"])
            got = I.item(src, x["id"])
            for c in got["choices"] + got.get("acts", []) + ([I.NOTE] if got.get("note", {}).get("open") else []):
                meaning = HOME.get(c["id"]) or HOME.get(re.sub(r":\d+$", ":{}", c["id"]))
                assert meaning and _check(meaning, c["label"]), (x["kind"], c)
    assert seen == set(ITEM_NAMES), seen
