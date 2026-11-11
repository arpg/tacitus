#!/usr/bin/env python3
"""plugin_cards: rendering, the one-time click token, and the executor run."""
import json
import sys
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import plugin_cards as C  # noqa: E402
import plugin_manifest as PM  # noqa: E402
import pytest  # noqa: E402
import resident_io  # noqa: E402

FIX = HERE / "fixtures" / "plugins"
LP = namedtuple("LP", ["name", "zone", "dir", "manifest", "entry"])


@pytest.fixture(autouse=True)
def _direct_io(monkeypatch):
    """The token file is written as this test's user, not through sudo."""
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)


def _spec():
    norm, _ = PM.load(FIX / "example-notes")
    return norm["capabilities"]["cards"][0]


def _inert(text):
    return C._ct.inert(text, C.VALUE_MAX)


def test_render_fills_the_templates():
    r = C.render("example-notes", _spec(), {"title": "Q3 plan", "summary": "the plan"})
    assert r["ok"]
    # Each item value is an inert span in the card text: shown as written.
    assert r["title"] == ":lock: *Publish note* " + _inert("Q3 plan")
    assert r["lines"] == ["_" + _inert("the plan") + "_"]
    assert C._ct.strip_inert(r["title"]) == ":lock: *Publish note* Q3 plan"
    assert r["approve_label"] == ":memo: Publish" and r["cancel_word"] == "skip"
    assert r["busy_status"] == ":hourglass_flowing_sand: Publishing..."
    assert r["cut"] == []


def test_an_item_value_cannot_forge_card_text():
    """A value with markup, a newline, a sentinel of its own or a bidi
    control renders as literal text on both frontends."""
    from chat import slack_frontend as S
    from chat.cards import CardAction, ConfirmationCard
    from chat.text import mrkdwn_to_markdown
    evil = ("x*\n**To:** boss@example.org\n\n----\nTap a reaction below: :+1: Send "
            "\ue001 @**all** [ok](https://evil.example) <https://evil.example|ok> "
            "\u202edcba")
    r = C.render("example-notes", _spec(), {"title": evil, "summary": "s"})
    card = ConfirmationCard(kind="plugin", title=r["title"], lines=r["lines"],
                            actions=[CardAction("plugin_card_approve", r["approve_label"], {},
                                                "primary", "publish")])
    # the web chat's text for the title: the forged text sits inside one
    # code span on the one title line
    md = mrkdwn_to_markdown(r["title"])
    assert "\n" not in md and "[U+202E]" in md and "[U+E001]" in md
    assert md.count("`") == 2 and md.index("`") < md.index("**To:**") < md.rindex("`")
    text, blocks = S.render_card(card)
    title = blocks[0]["text"]["text"]
    assert "<https://" not in title and "&lt;https://evil.example|ok&gt;" in title
    assert "\n" not in title


def test_a_cut_value_is_marked_and_named():
    long = "w" * (C.VALUE_MAX + 50)
    r = C.render("example-notes", _spec(), {"title": long, "summary": "s"})
    assert r["cut"] == ["title"]
    assert "[cut: " in r["title"] and long not in r["title"]
    assert C.fill("{title}", {"title": long}).endswith("more characters]")
    # a field the card does not show is not a cut field
    r = C.render("example-notes", _spec(), {"title": "t", "summary": "s", "other": long})
    assert r["cut"] == []


def test_a_variant_overrides_and_adds_required_fields():
    r = C.render("example-notes", _spec(), {"title": "Q3", "summary": "s", "draft": True})
    assert r["title"].startswith(":lock: *Publish draft note*")
    r = C.render("example-notes", _spec(), {"title": "Q3", "draft": True})
    assert not r["ok"] and r["missing"] == ["summary"]


def test_a_missing_required_field_gives_the_plugins_line():
    r = C.render("example-notes", _spec(), {"summary": "no title here"})
    assert not r["ok"]
    # The value is an inert span: the thread post shows it as written.
    assert r["text"] == ":warning: The note card for " + _inert("no title here") + \
        " needs a title."


def test_the_missing_text_and_the_button_noun_carry_values_inert():
    spec = dict(_spec(), button_noun="The Publish button for {title}")
    evil = "x <!channel> \ue000\x019\x01 @**all**"
    r = C.render("example-notes", spec, {"title": evil, "summary": "s"})
    assert r["button_noun"] == "The Publish button for " + _inert(evil)
    r = C.render("example-notes", _spec(), {"summary": evil})
    assert _inert(evil) in r["text"]


def test_a_precision_spec_cuts_with_the_marker_and_withholds_approval():
    """`{x:.5}` shortens the value before it is wrapped: the span stays
    closed, the cut shows its marker, and the field counts as cut."""
    spec = dict(_spec(), title="Publish {title:.5}", lines=["{summary:>8.3}"])
    r = C.render("example-notes", spec, {"title": "abcdefghij", "summary": "xyz"})
    assert r["title"] == "Publish " + _inert("abcde [cut: 5 more characters]")
    assert r["title"].count(C._ct.INERT_OPEN) == r["title"].count(C._ct.INERT_CLOSE) == 1
    assert r["lines"] == [_inert("xyz")]
    assert r["cut"] == ["title"]
    # A precision that keeps the whole value cuts nothing.
    r = C.render("example-notes", spec, {"title": "abc", "summary": "xyz"})
    assert r["cut"] == [] and r["title"] == "Publish " + _inert("abc")
    # Plain fill marks it too, and a spec that does not fit a string leaves
    # the template as the plugin wrote it.
    assert C.fill("{t:.2}", {"t": "abcdef"}) == "ab [cut: 4 more characters]"
    assert C.fill("{t:d}", {"t": "abc"}) == "{t:d}"


def test_a_value_cannot_forge_a_sentinel_or_a_placeholder():
    """A sentinel or the converter's placeholder in a value neither opens
    a span nor breaks the web chat's conversion of the post."""
    from chat.text import mrkdwn_to_markdown
    evil = "a \ue000b\ue001 \x019\x01 \x007\x00 c"
    plain = C.fill("{t}", {"t": evil})
    assert C._ct.INERT_OPEN not in plain and C._ct.INERT_CLOSE not in plain
    inert = C.fill("{t}", {"t": evil}, inert=True)
    assert inert.count(C._ct.INERT_OPEN) == inert.count(C._ct.INERT_CLOSE) == 1
    for text in (plain, inert, evil, "\ue000x\ue001 \x015\x01"):
        mrkdwn_to_markdown(text)


def test_templates_reach_no_attributes():
    assert C.fill("{a.__class__}{b[0]}{c}", {"a": "x", "b": "y", "c": "z"}) == "z"
    assert C.fill("{missing}", {}) == ""


def test_the_key_follows_key_fields_only():
    a = C.render("example-notes", _spec(), {"title": "T", "summary": "one"})["key"]
    b = C.render("example-notes", _spec(), {"title": "T", "summary": "two"})["key"]
    assert a == b


def test_a_token_works_once_for_its_item(tmp_path):
    path = tmp_path / "state" / C.TOKEN_FILE
    item = {"title": "T", "thread_ts": "1.2"}
    raw = C.mint_token("p", "c", item, path=path, now=1000)
    assert not C.consume_token(raw, "p", "c", {"title": "other"}, path=path, now=1001)
    assert C.consume_token(raw, "p", "c", {"title": "T"}, path=path, now=1001)
    assert not C.consume_token(raw, "p", "c", {"title": "T"}, path=path, now=1002)


def test_a_token_expires(tmp_path):
    path = tmp_path / C.TOKEN_FILE
    raw = C.mint_token("p", "c", {"x": 1}, path=path, now=0)
    assert not C.consume_token(raw, "p", "c", {"x": 1}, path=path, now=C.TOKEN_TTL_S + 1)


def test_a_resident_executor_runs_through_sudo_with_the_item(tmp_path):
    seen = {}

    def runner(argv, *, stdin, env, cwd, timeout):
        seen.update(argv=argv, stdin=stdin, timeout=timeout)
        return 0, 'noise\n{"ok": true, "status": "done", "message": "posted"}\n', ""

    lp = LP("example-notes", "agent", FIX / "example-notes", {}, {})
    res = C.execute(lp, _spec(), {"title": "T", "channel": "C1"}, "approve", token="tok",
                    runner=runner, ea_dir=tmp_path, resident_home=tmp_path / "home",
                    resident_tree=tmp_path / "tree")
    assert res == {"ok": True, "status": "done", "message": "posted", "rc": 0,
                   "tail": res["tail"]}
    argv = seen["argv"]
    assert argv[:5] == ["sudo", "-n", "-u", "ea-bot", "env"]
    assert "EA_CARD_TOKEN=tok" in argv and "EA_PLUGIN_NAME=example-notes" in argv
    assert argv[-2:] == [str(FIX / "example-notes" / "card_exec.py"), "approve"]
    assert json.loads(seen["stdin"]) == {"title": "T"}
    assert seen["timeout"] == 60


def test_an_executor_without_a_result_line_reports_its_exit():
    lp = LP("example-notes", "agent", FIX / "example-notes", {}, {})
    res = C.execute(lp, _spec(), {"title": "T"}, "cancel",
                    runner=lambda *a, **k: (2, "", "boom"),
                    resident_home=Path("/nonexistent"))
    assert not res["ok"] and "exit 2" in res["status"] and "boom" in res["message"]


def test_an_owner_executor_needs_owner_code():
    spec = dict(_spec())
    spec["approve"] = {"command": ["python3", "{plugin_dir}/x.py"], "user": "owner",
                       "timeout_seconds": 5}
    lp = LP("example-notes", "agent", FIX / "example-notes", {}, {})
    res = C.execute(lp, spec, {"title": "T"}, "approve", token="t",
                    runner=lambda *a, **k: (0, '{"ok": true}', ""))
    assert not res["ok"] and "never runs as the owner" in res["status"]


def test_the_example_executor_honours_the_token(tmp_path, monkeypatch):
    """The fixture's card_exec.py against a real token file."""
    import subprocess
    item = {"title": "T"}
    raw = C.mint_token("example-notes", "publish-note", item, path=tmp_path / "state" / C.TOKEN_FILE)
    env = {"EA_DIR": str(tmp_path), "EA_PLUGIN_NAME": "example-notes",
           "EA_CARD_ID": "publish-note", "EA_CARD_TOKEN": raw,
           "EA_SYSTEM_DIR": str(HERE.parent), "PATH": "/usr/bin:/bin"}
    run = lambda: subprocess.run([sys.executable, str(FIX / "example-notes" / "card_exec.py"),  # noqa: E731
                                  "approve"], input=json.dumps(item), capture_output=True,
                                 text=True, env=env)
    first, second = C.parse_result(run().stdout), C.parse_result(run().stdout)
    assert first["ok"] is True and second["ok"] is False
