#!/usr/bin/env python3
"""Plugin mail triggers: the manifest checks, the match, the hand-off the
tagger tick makes, and the plugin's read of what it was handed."""
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
sys.path.insert(0, str(HERE.parent))

import mail_tagger as mt  # noqa: E402
import plugin_mail as PMAIL  # noqa: E402
import plugin_manifest as PM  # noqa: E402
import plugin_registry as R  # noqa: E402
from ea_plugins import mail as plugin_side  # noqa: E402

FIX = HERE / "fixtures" / "plugins"
NOW = datetime(2026, 10, 18, 15, 40, tzinfo=timezone.utc)
ALERTS = "alerts@grants.example.org"
COLLEAGUE = "quinn.halvorsen17@example.edu"
CFG = {"mail": "enforce", "keep_from_anthropic": {"addresses": [], "domains": ["kept.example.net"]}}


@pytest.fixture(autouse=True)
def _direct_writes(monkeypatch):
    """The resident's inbox is written directly, not through a child run
    as the resident: the test's temp dir is the test user's."""
    monkeypatch.setattr(PMAIL._rio, "_owner_check", lambda: False)


def _raw(triggers):
    raw = json.loads((FIX / "example-notes" / "plugin.json").read_text())
    raw["capabilities"]["mail_triggers"] = triggers
    return raw


def _msg(i, frm=ALERTS, subject="New funding matches", cats=("auto-tagged", "fyi"),
         to=("wren.fairbanks84@example.edu",)):
    return {"id": f"m{i}", "subject": subject, "conversationId": f"c{i}",
            "from": {"emailAddress": {"address": frm, "name": "Alerts"}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [], "receivedDateTime": "2026-10-18T15:33:00Z",
            "categories": list(cats)}


class Graph:
    def __init__(self, bulk=(), fail=()):
        self.bulk, self.fail, self.gets = set(bulk), set(fail), []

    def graph_get(self, url, params=None):
        mid = url.rsplit("/", 1)[-1]
        self.gets.append(mid)
        if mid in self.fail:
            raise RuntimeError("503")
        headers = [{"name": "List-Unsubscribe", "value": "<mailto:u@x>"}] if mid in self.bulk else \
            [{"name": "Received", "value": "from somewhere"}]
        return {"id": mid, "internetMessageId": f"<{mid}@example.org>",
                "internetMessageHeaders": headers,
                "body": {"contentType": "html", "content": f"<p>body of {mid}</p>"}}


PIVOT = {"id": "alerts", "from": [ALERTS], "topics": [], "subject": [], "exclude_from": [],
         "bulk": False}
SOLICIT = {"id": "solicitations", "from": [], "topics": ["funding"], "subject": [],
           "exclude_from": [ALERTS], "bulk": True}


def _route(msgs, d, triggers, graph=None, **kw):
    kw.setdefault("cfg", CFG)
    return PMAIL.route(msgs, graph=graph or Graph(), now=NOW, triggers=triggers,
                       base=Path(d), ea_dir=Path(d) / "ea", **kw)


def _inbox(d, plugin="funding"):
    p = PMAIL.inbox_path(plugin, Path(d) / "ea")
    return json.loads(p.read_text()) if p.exists() else []


# ── manifest ─────────────────────────────────────────────────────────

def test_triggers_normalize_and_enter_the_consent(d):
    norm, errors = PM.normalize(_raw([
        {"id": "alerts", "from": ["Alerts@Grants.example.org"]},
        {"id": "solicitations", "topics": ["funding"], "bulk": True,
         "exclude_from": [ALERTS], "subject": ["funding"]}]))
    assert errors == []
    t = norm["capabilities"]["mail_triggers"]
    assert t[0] == PIVOT
    assert t[1]["topics"] == ["funding"] and t[1]["bulk"] and t[1]["subject"] == ["funding"]
    summary = PM.capability_summary(norm)
    assert summary["mail_triggers"] == t
    text = "\n".join(PM.describe(summary))
    assert "mail trigger alerts: new mail from alerts@grants.example.org" in text
    assert "tagged funding" in text and "bulk mail only" in text


def test_a_plugin_without_triggers_keeps_its_accepted_digest(d):
    norm, _ = PM.load(FIX / "example-notes")
    summary = PM.capability_summary(norm)
    assert "mail_triggers" not in summary
    # A lock entry written before triggers existed has no key at all.
    old = copy.deepcopy(norm)
    del old["capabilities"]["mail_triggers"]
    assert PM.capability_digest(PM.capability_summary(old)) == PM.capability_digest(summary)
    with_t, _ = PM.normalize(_raw([{"id": "alerts", "from": [ALERTS]}]))
    assert PM.capability_digest(PM.capability_summary(with_t)) != PM.capability_digest(summary)


def test_bad_triggers_are_refused(d):
    cases = [
        ([{"id": "t", "topics": ["funding"]}], "must set"),            # topic alone, not bulk
        ([{"id": "t", "from": ["nobody"]}], "address or an @domain"),
        ([{"id": "t", "from": ["@localhost"]}], "address or an @domain"),
        ([{"id": "t", "topics": ["gossip"], "bulk": True}], "not one of"),
        ([{"id": "t", "bulk": True}], "needs `from` or `topics`"),
        ([{"id": "t", "from": [ALERTS], "subject": ["re"]}], "too broad"),
        ([{"id": "t", "from": [ALERTS]}, {"id": "t", "from": [ALERTS]}], "unique slug"),
        ({"id": "t"}, "must be a list"),
    ]
    for triggers, needle in cases:
        _, errors = PM.normalize(_raw(triggers))
        assert any(needle in e for e in errors), (triggers, errors)


def test_the_topic_list_is_the_taggers(d):
    assert PM.MAIL_TOPICS == mt.TOPICS


def test_registry_hands_out_triggers_of_verified_plugins_only(d):
    norm, _ = PM.normalize(_raw([{"id": "alerts", "from": [ALERTS]}]))
    lp = R.LockedPlugin(name="example-notes", zone="agent", dir=Path(d), manifest=norm, entry={})
    drifted = lp._replace(name="drifted")
    plain, _ = PM.load(FIX / "example-notes")
    other = R.LockedPlugin(name="other", zone="agent", dir=Path(d), manifest=plain, entry={})
    hashed = []

    def verify(p, use_cache=True):
        hashed.append(p.name)
        return ["skill.md: changed since install"] if p.name == "drifted" else []

    real = R.managed_plugins, R.verify
    try:
        R.managed_plugins = lambda lock=None, **kw: [lp, drifted, other]
        R.verify = verify
        got = R.mail_triggers()
    finally:
        R.managed_plugins, R.verify = real
    assert [(p.name, t["id"]) for p, t in got] == [("example-notes", "alerts")]
    assert hashed == ["example-notes", "drifted"]


# ── matching and delivery ────────────────────────────────────────────

def test_a_sender_trigger_copies_the_mail_with_its_body(d):
    s = _route([_msg(1), _msg(2, frm=COLLEAGUE)], d, [("funding", "agent", PIVOT)])
    assert s["delivered"] == 1
    inbox = _inbox(d)
    assert [i["id"] for i in inbox] == ["m1"]
    rec = inbox[0]
    assert rec["trigger"] == "alerts" and rec["body"] == "<p>body of m1</p>"
    assert rec["internet_message_id"] == "<m1@example.org>" and rec["from"] == ALERTS
    # the owner keeps the canonical queue; the resident's copy is its own
    assert json.loads(PMAIL.queue_path("funding", Path(d)).read_text()) == inbox
    assert oct(PMAIL.inbox_path("funding", Path(d) / "ea").stat().st_mode & 0o777) == "0o640"


def test_a_failed_delivery_is_tried_again(d):
    real = PMAIL._rio.replace_text
    try:
        PMAIL._rio.replace_text = lambda path, text, mode=0o664: False
        s = _route([_msg(1)], d, [("funding", "agent", PIVOT)])
    finally:
        PMAIL._rio.replace_text = real
    assert s["delivered"] == 0 and _inbox(d) == []
    s = _route([_msg(1)], d, [("funding", "agent", PIVOT)])
    assert s["delivered"] == 1 and len(_inbox(d)) == 1


def test_a_domain_entry_matches_the_domain_only(d):
    t = dict(PIVOT, **{"from": ["@grants.example.org"]})
    msgs = [_msg(1, frm="news@grants.example.org"), _msg(2, frm="news@notgrants.example.org")]
    _route(msgs, d, [("funding", "agent", t)])
    assert [i["id"] for i in _inbox(d)] == ["m1"]


def test_a_topic_trigger_needs_bulk_mail_and_honours_exclusions(d):
    tagged = ("auto-tagged", "fyi", "funding")
    msgs = [_msg(1, frm="news@agency.example.gov", cats=tagged),        # bulk: yes
            _msg(2, frm=COLLEAGUE, cats=tagged),                         # a person: no
            _msg(3, frm=ALERTS, cats=tagged),                            # excluded sender
            _msg(4, frm="news@agency.example.gov", cats=("auto-tagged", "fyi"))]  # no topic
    g = Graph(bulk={"m1", "m2", "m3", "m4"} - {"m2"})
    _route(msgs, d, [("funding", "agent", SOLICIT)], graph=g)
    assert [i["id"] for i in _inbox(d)] == ["m1"]
    # only the prematched mail cost a fetch
    assert sorted(g.gets) == ["m1", "m2"]


def test_fresh_tags_win_and_untagged_mail_waits(d):
    m = _msg(1, frm="news@agency.example.gov", cats=())
    g = Graph(bulk={"m1"})
    s = _route([m], d, [("funding", "agent", SOLICIT)], graph=g)
    assert s["delivered"] == 0 and g.gets == []
    s = _route([m], d, [("funding", "agent", SOLICIT)], graph=g,
               cats_by_id={"m1": ["auto-tagged", "fyi", "funding"]})
    assert s["delivered"] == 1


def test_with_the_tagger_off_sender_triggers_still_run(d):
    s = _route([_msg(1, cats=())], d, [("funding", "agent", PIVOT)], tagged_only=False)
    assert s["delivered"] == 1


def test_a_message_is_handed_over_once(d):
    g = Graph()
    _route([_msg(1)], d, [("funding", "agent", PIVOT)], graph=g)
    s = _route([_msg(1)], d, [("funding", "agent", PIVOT)], graph=g)
    assert s["matched"] == 0 and g.gets == ["m1"] and len(_inbox(d)) == 1


def test_a_failed_fetch_is_tried_again(d):
    s = _route([_msg(1)], d, [("funding", "agent", PIVOT)], graph=Graph(fail={"m1"}))
    assert s["delivered"] == 0
    s = _route([_msg(1)], d, [("funding", "agent", PIVOT)])
    assert s["delivered"] == 1


def test_kept_mail_never_reaches_an_agent_plugin(d):
    m = _msg(1, to=("someone@kept.example.net",))
    s = _route([m], d, [("funding", "agent", PIVOT), ("reader", "owner", PIVOT)])
    assert s["kept"] == 1 and _inbox(d) == []
    owner_q = json.loads(PMAIL.queue_path("reader", Path(d)).read_text())
    assert [i["id"] for i in owner_q] == ["m1"] and owner_q[0]["body"]
    assert not PMAIL.inbox_path("reader", Path(d) / "ea").exists()


def test_sealed_mail_reaches_an_agent_plugin_without_its_body(d):
    cfg = dict(CFG, mail="sealed")
    _route([_msg(1)], d, [("funding", "agent", PIVOT)], cfg=cfg)
    rec = _inbox(d)[0]
    assert rec["sealed"] is True and "body" not in rec


def test_dry_run_writes_nothing(d):
    s = _route([_msg(1)], d, [("funding", "agent", PIVOT)], dry_run=True)
    assert s["matched"] == 1 and s["delivered"] == 0
    assert not PMAIL.queue_path("funding", Path(d)).exists()
    assert not PMAIL.seen_path(Path(d)).exists()


def test_old_copies_age_out(d):
    old = {"id": "m0", "received": "2026-08-01T00:00:00Z",
           "delivered_at": PMAIL._iso(NOW - PMAIL.RETAIN - timedelta(days=1))}
    PMAIL.queue_path("funding", Path(d)).write_text(json.dumps([old]))
    _route([_msg(1)], d, [("funding", "agent", PIVOT)])
    assert [i["id"] for i in _inbox(d)] == ["m1"]


# ── the tagger tick ──────────────────────────────────────────────────

class TagGraph:
    def __init__(self):
        self.patches = []

    def graph_patch(self, endpoint, body):
        self.patches.append(endpoint)
        return {}

    def graph_get(self, url, params=None):
        return {"value": []}


def test_the_tick_routes_with_the_tags_it_just_gave(d):
    seen = {}

    def router(inbox, **kw):
        seen.update(kw, ids=[m["id"] for m in inbox])
        return {"delivered": 1, "matched": 1}

    def asker(prompt):
        return {"0": {"project": "research", "state": "fyi", "topic": "funding"}}, {}

    s = mt.tick(graph=TagGraph(), now=NOW, state_path=Path(d) / "st.json",
                log_path=Path(d) / "log.jsonl", inbox=[_msg(1, cats=())], sent=[],
                asker=asker, vocab=[], router=router)
    assert s["route"]["delivered"] == 1 and seen["ids"] == ["m1"]
    assert "funding" in seen["cats_by_id"]["m1"] and seen["tagged_only"] is True


def test_a_routing_failure_does_not_stop_the_tick(d):
    def router(inbox, **kw):
        raise RuntimeError("lock unreadable")

    s = mt.tick(graph=TagGraph(), now=NOW, state_path=Path(d) / "st.json",
                log_path=Path(d) / "log.jsonl", inbox=[], sent=[], asker=lambda p: ({}, {}),
                vocab=[], router=router)
    assert "lock unreadable" in s["route"]["error"]
    assert json.loads((Path(d) / "st.json").read_text())["last_tick"]


def test_with_the_tagger_off_the_tick_only_routes(d):
    calls = []

    def asker(prompt):
        calls.append(prompt)
        return {}, {}

    s = mt.tick(graph=TagGraph(), now=NOW, state_path=Path(d) / "st.json",
                log_path=Path(d) / "log.jsonl", inbox=[_msg(1, cats=())], asker=asker,
                vocab=[], tag=False, router=lambda inbox, **kw: {"tagged_only": kw["tagged_only"]})
    assert calls == [] and "tag" not in s and s["route"] == {"tagged_only": False}


# ── the plugin's read ────────────────────────────────────────────────

def test_delivered_reads_the_copies_in_graph_shape(d):
    _route([_msg(1), _msg(2, subject="Other")], d, [("funding", "agent", PIVOT)])
    _route([_msg(3)], d, [("funding", "agent", PIVOT)], cfg=dict(CFG, mail="sealed"))
    old_env = os.environ.get("EA_DIR")
    old_get = plugin_side.get_message
    try:
        os.environ["EA_DIR"] = str(Path(d) / "ea")
        plugin_side.get_message = lambda mid, raw_html=True: {
            "body_type": "text", "body": f"record of {mid}"}
        got = plugin_side.delivered(plugin="funding")
    finally:
        plugin_side.get_message = old_get
        if old_env is None:
            os.environ.pop("EA_DIR", None)
        else:
            os.environ["EA_DIR"] = old_env
    assert sorted(m["id"] for m in got) == ["m1", "m2", "m3"]
    by = {m["id"]: m for m in got}
    assert by["m1"]["body"]["content"] == "<p>body of m1</p>"
    assert by["m1"]["from"]["emailAddress"]["address"] == ALERTS
    assert by["m1"]["trigger"] == "alerts" and by["m1"]["conversationId"] == "c1"
    assert by["m3"]["body"] == {"contentType": "text", "content": "record of m3"}


if __name__ == "__main__":
    import tempfile
    import traceback
    PMAIL._rio._owner_check = lambda: False
    failed = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_")):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fn(tmp)
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
