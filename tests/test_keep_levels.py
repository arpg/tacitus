"""Keep levels (privacy_rules.py, mail_privacy, mail_gate): the rename of
the keep list, the four levels and their views, the matchers and their
order, the rules in the preferences store, the engaged-person store, the
mediator per level and lane, and every lane where a hosted model (Claude,
Codex or any other) could meet kept mail. Made-up people and domains only;
nothing reaches the network or the real private dir."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402
import mail_gate as mg  # noqa: E402
import model_routing as mr  # noqa: E402
import preferences as prefs  # noqa: E402
import privacy_rules as pr  # noqa: E402

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
ME = "owner@example.edu"
APPLICANT = "morgan.applicant@mail.example.net"
COLLEAGUE = "pat@partner.example.org"
APPLY_SUBJECT = "Prospective PhD student, Fall 2027"
APPLY_BODY = ("Dear Professor, I am a prospective PhD student and I am seeking PhD positions in "
              "your lab. My CV is attached. My phone is 555-0100.")
ACCEPT_BODY = "Thank you for your reply. Tuesday at 2:15 pm works for me, see you then."
RECORD = {"sensitivity": "normal", "confidential_kind": "none", "needs_reply": True,
          "ask": "Asks to join the lab.", "deadline": "2026-11-20", "deadline_text": "by Nov 20",
          "scheduling": "none", "offered_times": [], "people": [{"name": "Morgan", "role": "student"}],
          "tone": [], "state": "needs-reply", "domain": "research",
          "summary": "A prospective student asks about the lab."}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_cfg(d, **over):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_frontier": {"domains": ["mil"], "addresses": [], "admissions": True}}
    cfg.update(over)
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    pr.reset()


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
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {ME})
    monkeypatch.setattr(mp, "_profile_line", lambda: "The user is a professor.")
    monkeypatch.setattr(mg, "_list_senders", lambda: set())
    _write_cfg(d)
    return d


class FakeGraph:
    MAIL_FIELDS = "id"

    def __init__(self, bodies):
        self.bodies = bodies

    def get_message_full(self, mid):
        return {"body": {"contentType": "text", "content": self.bodies[mid]}}

    def get_attachments(self, mid):
        return []


def _chat(result=RECORD):
    return lambda messages, **kw: (dict(result), {"latency_ms": 5})


def _msg(mid, conv, frm=APPLICANT, subject=APPLY_SUBJECT, cc=(), received=None, name="Morgan Applicant"):
    return {"id": mid, "conversationId": conv, "receivedDateTime": received or _iso(NOW - timedelta(hours=2)),
            "subject": subject, "hasAttachments": False, "isDraft": False,
            "from": {"emailAddress": {"name": name, "address": frm}},
            "toRecipients": [{"emailAddress": {"address": ME}}],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}


def _extract(msg, body, result=RECORD, own=False):
    rec = mp.process(msg, graph=FakeGraph({msg["id"]: body}), cfg=rl.config(), chat=_chat(result), own=own)
    rl.append(mp.EXTRACTIONS, rec)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    return rec


def _rules(*rows):
    """A preferences store holding these rules (with the built-ins seeded)."""
    data = {"version": 1, "rules": [], "calibration": []}
    pr.seed(data)
    for r in rows:
        prefs.add_rule(data, kind="instruction", scopes=["privacy"], text=r["text"], quote=r.get("quote", "x"),
                       source="chat", match=r.get("match", ""), level=r.get("level", ""),
                       date_str=r.get("date", "2026-11-01"))
    prefs.save(data)
    pr.reset()
    return data


# ── the rename ────────────────────────────────────────────────────────

def test_the_old_key_is_read_with_a_note_and_the_doctor_asks_for_the_rename(env, capsys):
    cfg = json.loads((env / rl.CONFIG_FILE).read_text())
    cfg["keep_from_anthropic"] = cfg.pop("keep_from_frontier")
    owner_trust.write_owner_file(env / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    rl._LEGACY_NOTED.clear()
    got = rl.config()
    assert got["keep_from_frontier"]["domains"] == ["mil"] and got["keep_legacy_key"] is True
    assert rl.keep_reason("someone@army.mil", got) == "listed domain mil"
    assert "Rename the key" in capsys.readouterr().err
    status, _detail, fix = pr.doctor_row(got)
    assert status == "WARN" and fix.startswith("rename keep_from_anthropic to keep_from_frontier in remote-llm.json")
    # A rewrite of the file (init) carries the list under the new name.
    rl.init_config("https://llm.example.edu/v1", "m-1")
    raw = json.loads((env / rl.CONFIG_FILE).read_text())
    assert "keep_from_anthropic" not in raw and raw["keep_from_frontier"]["domains"] == ["mil"]
    assert "keep_legacy_key" not in rl.config()
    # A caller's own config dict under the old name still reads.
    assert rl.keep_list({"keep_from_anthropic": {"domains": ["x.example"]}})["domains"] == ["x.example"]
    assert mr.keep_listed({"keep_from_anthropic": {"admissions": True}})


def test_records_are_written_under_the_new_name_and_both_names_read(env):
    rec = _extract(_msg("m1", "c1", frm="someone@army.mil", subject="Hello"), "Hello there.")
    assert rec["keep_from_frontier"] == "listed domain mil" and "keep_from_anthropic" not in rec
    assert rec["keep_level"] == "local" and rec["keep_facts"]["listed"] == "listed domain mil"
    old = {"id": "m2", "conversation_id": "c2", "from": "x@example.com", "status": "ok",
           "keep_from_anthropic": "admissions", "result": dict(RECORD)}
    assert mp.kept_reason(old) == "admissions" and mp.level_of(old, index={"by_conv": {}}) == "local"
    assert mp.kept_label({"kept_from_claude": "mil"}) == "mil"
    assert mp.kept_label({"kept_from_frontier": "mil"}) == "mil"


def test_is_hosted_is_a_class_check():
    for name in ("claude", "codex", "claude:opus", "gpt-6.1-sol", "", "unknown", "some-new-model"):
        assert mr.is_hosted(name), name
    for name in ("local", "remote", "LOCAL", "local:qwen-27b"):
        assert not mr.is_hosted(name), name


# ── the matchers and their order ──────────────────────────────────────

def _decide(facts, sender=APPLICANT, conv="c1", own=False, **ctx_over):
    ctx = pr.build_context(rl.config(), data=ctx_over.pop("data", None), own_convs=ctx_over.pop("own_convs", ()),
                           engaged=ctx_over.pop("engaged", ()), people=ctx_over.pop("people", ()), user={ME},
                           af_ctx=ctx_over.pop("af_ctx", None))
    return pr.decide(facts, sender=sender, name=facts.get("name", ""), conv=conv, own=own, ctx=ctx)


def test_each_matcher_and_the_order(env):
    applicant = pr.message_facts(_msg("m1", "c1"), APPLY_BODY, rl.config())
    assert applicant["topic"] and applicant["score"] >= 2 and not applicant["listed"]
    # topic: applicant mail stays on the machine; its files at every level
    d = _decide(applicant)
    assert (d["level"], d["reason"], d["rule"]) == ("local", "admissions", "privacy-applicant-mail")
    assert d["files_kept"]
    # engaged: the user wrote in the thread, to the person, keeps them in a
    # project, or an admissions exemption fired: summary, the ceiling
    for kw in ({"own_convs": {"c1"}}, {"engaged": {APPLICANT}}, {"people": {APPLICANT}}):
        d = _decide(applicant, **kw)
        assert (d["level"], d["rule"]) == ("summary", "privacy-engaged-applicants"), kw
        assert d["files_kept"] and d["engaged"]
    d = _decide(dict(applicant, exempt="answers something the user wrote"))
    assert d["level"] == "summary"
    # participant: the keep list wins over everything, a person rule included
    _rules({"text": "Treat Morgan as a normal correspondent.", "match": f"person:{APPLICANT}", "level": "full"})
    listed = pr.message_facts(_msg("m2", "c2", cc=["officer@navy.mil"]), APPLY_BODY, rl.config())
    assert _decide(listed)["level"] == "local" and _decide(listed)["rule"] == "privacy-keep-list"
    # person: the user's rule outranks the topic rule, and the files still stay
    d = _decide(applicant)
    assert d["level"] == "full" and d["reason"] == "" and d["files_kept"]
    # ordinary mail is full
    plain = pr.message_facts(_msg("m3", "c3", frm=COLLEAGUE, subject="Lunch"), "See you at noon.", rl.config())
    assert not plain["topic"] and _decide(plain, sender=COLLEAGUE)["level"] == "full"
    # an internal sender is never an applicant
    internal = pr.message_facts(_msg("m4", "c4", frm="dean@example.edu"), APPLY_BODY, rl.config())
    assert not internal["topic"]


def test_rules_the_user_gives_and_what_they_may_not_open(env):
    _rules({"text": "Keep everything from partner.example.org on my machine.", "match": "domain:partner.example.org",
            "level": "local"},
           {"text": "Facts only for applicants I answered.", "match": "engaged", "level": "facts",
            "date": "2026-11-02"})
    plain = pr.message_facts(_msg("m3", "c3", frm=COLLEAGUE, subject="Lunch"), "See you at noon.", rl.config())
    assert _decide(plain, sender=COLLEAGUE)["level"] == "local"
    applicant = pr.message_facts(_msg("m1", "c1"), APPLY_BODY, rl.config())
    assert _decide(applicant, own_convs={"c1"})["level"] == "facts", "the user's engaged rule outranks the default"
    with pytest.raises(ValueError, match="opens mail names one person"):
        prefs.privacy_matcher("domain:partner.example.org", "full")
    with pytest.raises(ValueError, match="needs --match and --level"):
        prefs.privacy_matcher(text="Be careful with mail.")
    assert prefs.privacy_matcher(text="Treat pat@partner.example.org as a normal correspondent.") == \
        {"match": "person", "value": "pat@partner.example.org", "level": "full"}
    assert prefs.privacy_matcher(text="Keep everything from army.example.mil on my machine.") == \
        {"match": "domain", "value": "army.example.mil", "level": "local"}
    with pytest.raises(ValueError, match="belong to a privacy rule"):
        prefs.add_rule({"rules": []}, kind="prefer", scopes=["briefing"], text="x", match="topic", level="local")


def test_seeding_listing_retiring_and_a_user_rule_outranking_a_default(env, capsys):
    assert prefs.main(["list", "--scope", "privacy"]) == 0
    out = capsys.readouterr().out
    for rid in pr.DEFAULT_IDS:
        assert rid in out
    assert "Mail from prospective students stays on your machine." in out
    assert pr.ensure_seeded() == [], "seeded once"
    # the keep-list rule follows the private config and cannot be retired
    assert prefs.main(["retire", "--id", "privacy-keep-list"]) == 2
    assert "change the list there" in capsys.readouterr().out
    # retiring the topic rule makes applicant mail ordinary mail
    assert prefs.main(["retire", "--id", "privacy-applicant-mail"]) == 0
    pr.reset()
    applicant = pr.message_facts(_msg("m1", "c1"), APPLY_BODY, rl.config())
    d = _decide(applicant)
    assert d["level"] == "full" and d["files_kept"], "the files rule stands on its own"
    # a rule added in chat outranks the defaults
    assert prefs.main(["add", "--kind", "instruction", "--scope", "privacy",
                       "--text", "Keep mail from Morgan on my machine.", "--quote", "keep Morgan's mail local",
                       "--match", f"person:{APPLICANT}", "--level", "local", "--source", "chat"]) == 0
    pr.reset()
    assert _decide(applicant, own_convs={"c1"})["level"] == "local"
    lines = pr.describe(pr.build_context(rl.config(), own_convs=(), engaged=(), people=(), user={ME}))
    assert any(APPLICANT in ln and "Kept on your machine" in ln for ln in lines)


def test_the_onboarding_answer_records_a_keep_rule(env):
    import onboarding
    q = onboarding.bank_by_id()["applicant-mail"]
    assert q["scope"] == "privacy" and q["question"].endswith("?")
    assert onboarding.privacy_answer(q, "3")[0] == "summary"
    assert onboarding.privacy_answer(q, "keep it on my machine")[0] == "local"
    assert onboarding.privacy_answer(q, "facts only please")[0] == "facts"
    with pytest.raises(ValueError, match="must pick one level"):
        onboarding.privacy_answer(q, "whatever you think")
    res = onboarding.record_answer("applicant-mail", "2", ea_dir=Path(env.parent / "ea"))
    rule = res["rule"]
    assert rule["privacy"] == {"match": "engaged", "level": "facts"} and rule["quote"] == "2"
    assert rule["text"].startswith("Once you have written to an applicant, the assistant sees only")
    # waits for a local model: none in a test process that cannot read the config
    assert not onboarding.available(dict(q)) or onboarding._local_model()


# ── the engaged-person store ──────────────────────────────────────────

def test_the_engaged_store_starts_a_new_thread_at_summary(env):
    assert pr.note_engaged({APPLICANT: "2026-11-01T10:00:00Z", "bad": "x"}) == 1
    assert pr.load_engaged() == {APPLICANT: "2026-11-01T10:00:00Z"}
    assert pr.note_engaged({APPLICANT: "2026-11-02T10:00:00Z"}) == 0
    pr.reset()
    rec = _extract(_msg("m9", "c-new"), APPLY_BODY)
    assert rec["keep_level"] == "summary" and rec["keep_from_frontier"] == "admissions"


def test_the_tick_records_whom_the_user_wrote_to(env):
    st = {"engaged_seeded": "2026-11-01T00:00:00Z"}
    own = dict(_msg("s1", "c1", frm=ME, subject="Re: " + APPLY_SUBJECT), sentDateTime="2026-11-03T15:00:00Z")
    own["toRecipients"] = [{"emailAddress": {"address": APPLICANT}}]
    assert mp._note_engaged(st, [own], {ME}, graph=None, now=NOW) == 1
    assert APPLICANT in pr.load_engaged()


# ── FACT_FIELDS ───────────────────────────────────────────────────────

def test_a_facts_view_keeps_the_fact_fields_and_nothing_else():
    item = {k: f"value of {k}" for k in mp.FACT_FIELDS}
    text = {"body_preview": "RAW", "preview": "RAW", "text": "RAW", "body": "RAW", "ask": "RAW ask",
            "summary": "RAW", "quotes": ["RAW"], "notes": "RAW", "deadline_text": "RAW", "latest_preview": "RAW",
            "gist": "RAW", "links": ["https://raw.example.com"], "bodyPreview": "RAW", "uniqueBody": "RAW"}
    item.update(text)
    item["remote_record"] = {"state": "todo", "needs_reply": True, "ask": "RAW", "summary": "RAW",
                             "quotes": ["RAW"], "people": [{"name": "RAW"}], "deadline": "2026-11-20",
                             "attachments": [{"name": "cv.pdf", "type": "application/pdf", "size": 9,
                                              "text_chars": 4000, "error": "RAW"}]}
    item["attachments"] = [{"name": "cv.pdf", "contentBytes": "RAW", "size": 9}]
    view = mp.facts_view(item)
    assert set(view) <= mp.FACT_FIELDS
    assert not any(k in view for k in text)
    assert set(view["remote_record"]) <= set(mp.RECORD_FACT_FIELDS)
    assert view["remote_record"]["attachments"] == [{"name": "cv.pdf", "type": "application/pdf", "size": 9}]
    assert view["attachments"] == [{"name": "cv.pdf", "size": 9}]
    assert "RAW" not in json.dumps(view)


# ── the gate: rows, threads and cards by level ────────────────────────

def _gate(msgs, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("graph", object())
    return mg.Gate(msgs, label="test", cfg=rl.config(), **kw)


def test_the_gate_passes_each_level_as_its_view(env):
    local = _msg("m1", "c1")
    _extract(local, APPLY_BODY)
    pr.note_engaged({"engaged.applicant@mail.example.net": "2026-11-01T00:00:00Z"})
    summ = _msg("m2", "c2", frm="engaged.applicant@mail.example.net")
    _extract(summ, APPLY_BODY)
    facts = _msg("m3", "c3", frm=COLLEAGUE, subject="Visit")
    _rules({"text": "Facts only for Pat.", "match": f"person:{COLLEAGUE}", "level": "facts"})
    _extract(facts, "Can we meet Tuesday at 2? My number is 555-0100.")
    items = [{"message_id": m["id"], "conversation_id": m["conversationId"], "subject": m["subject"],
              "body_preview": "RAW TEXT", "ask": "RAW ask"} for m in (local, summ, facts)]
    g = _gate([local, summ, facts])
    out = {o["message_id"]: o for o in g.items(items)}
    assert "m1" not in out, "kept on the machine: left out"
    s = out["m2"]
    assert s["kept_level"] == "Summary" and s["body_preview"].startswith("A prospective student asks")
    assert "quotes" not in s["remote_record"] and "555-0100" not in json.dumps(s)
    f = out["m3"]
    assert f["kept_level"] == "Facts only" and set(f) <= mp.FACT_FIELDS
    assert "RAW" not in json.dumps(f) and "A prospective student" not in json.dumps(f)
    assert f["remote_record"]["deadline"] == "2026-11-20" and "ask" not in f["remote_record"]
    assert g.counts["kept"] == 1 and g.counts["kept_summary"] == 1 and g.counts["kept_facts"] == 1


def test_the_regression_thread_reaches_the_scheduling_sweep_at_summary(env):
    """An applicant writes, the user offers times, the applicant takes one.
    The acceptance scores as applicant mail; the engaged rule lifts the
    thread to summary, the earlier message too, and the sweep gets it with
    the record's text only."""
    from preprocessor import preprocess as pp
    first = _msg("m-first", "conv-1", received=_iso(NOW - timedelta(days=3)))
    _extract(first, APPLY_BODY)
    assert mp.level_of(mp.load_index()["by_id"]["m-first"]) == "local"
    own = _msg("m-own", "conv-1", frm=ME, subject="Re: " + APPLY_SUBJECT, received=_iso(NOW - timedelta(days=2)))
    _extract(own, "Happy to talk. Would Tuesday at 2:15 pm or Wednesday at 10 am work?",
             result=dict(RECORD, ask="Pick one of the times.", offered_times=["2026-11-10T14:15:00"]), own=True)
    st = mp.load_state()
    st["own_sent"] = {"conv-1": _iso(NOW - timedelta(days=2))}
    mp.save_state(st)
    pr.reset()
    accept = _msg("m-accept", "conv-1", subject="Re: " + APPLY_SUBJECT, received=_iso(NOW - timedelta(hours=1)))
    rec = _extract(accept, ACCEPT_BODY + " " + APPLY_BODY,
                   result=dict(RECORD, state="fyi", needs_reply=False, scheduling="confirmation",
                               offered_times=["2026-11-10T14:15:00"], ask="",
                               summary="Morgan takes Tuesday at 2:15 pm."))
    assert rec["keep_facts"]["topic"] and rec["keep_level"] == "summary"
    index = mp.load_index()
    assert mp.level_of(index["by_id"]["m-first"], index=index) == "summary", "the earlier message rises too"
    assert mp.level_of(index["by_id"]["m-own"], index=index) == "summary"
    found = [{"message_id": "m-accept", "conversation_id": "conv-1", "subject": accept["subject"],
              "from": APPLICANT, "kind": "confirmation", "offered_times": ["2026-11-10T14:15:00"],
              "body_preview": "RAW " + ACCEPT_BODY, "acceptance": {"event": {"start": "2026-11-10T14:15:00-07:00"}}}]
    g = _gate([first, accept])
    got = pp.lane_scheduling(found, g)
    assert len(got) == 1 and got[0]["kept_level"] == "Summary" and not got[0].get("kept")
    assert got[0]["body_preview"] == mg.remote_text(rec) and "RAW" not in json.dumps(got[0])
    assert got[0]["acceptance"]["event"]["start"].startswith("2026-11-10")
    # the thread brief names the level for the row
    brief = mp.thread_brief("conv-1", index, user={ME})
    assert brief["kept_level"] == "Summary" and brief["keep_level"] == "summary"
    assert "kept_from_frontier" not in brief and all(m.get("level") == "Summary" for m in brief["messages"])
    import prompt_logic
    assert "kept: Summary" in prompt_logic.thread_brief_line(brief)


def test_the_thread_brief_at_facts_has_no_gist_ask_or_quote(env):
    _rules({"text": "Facts only for Pat.", "match": f"person:{COLLEAGUE}", "level": "facts"})
    m = _msg("m3", "c3", frm=COLLEAGUE, subject="Visit")
    _extract(m, "Can we meet Tuesday at 2? My number is 555-0100.")
    brief = mp.thread_brief("c3", user={ME})
    [msg] = brief["messages"]
    assert msg["level"] == "Facts only" and "gist" not in msg and msg["ask"] == "" and "quotes" not in msg
    assert brief["ask"] == "" and brief["deadline"] == "2026-11-20" and brief["kept_level"] == "Facts only"


# ── the mediator, per level and lane ──────────────────────────────────

def _read(mid, conv, body="RAW BODY 555-0100", frm=APPLICANT):
    return json.dumps({"id": mid, "subject": APPLY_SUBJECT, "from": {"address": frm}, "to": [ME], "cc": [],
                       "received": _iso(NOW), "body": body, "body_type": "text", "conversation_id": conv})


def test_the_mediator_per_level_and_lane(env):
    _extract(_msg("m1", "c1"), APPLY_BODY)                                   # local
    _rules({"text": "Facts only for Pat.", "match": f"person:{COLLEAGUE}", "level": "facts"})
    _extract(_msg("m3", "c3", frm=COLLEAGUE, subject="Visit"), "Tuesday at 2?")  # facts
    pr.note_engaged({"engaged.applicant@mail.example.net": "2026-11-01T00:00:00Z"})
    pr.reset()
    _extract(_msg("m2", "c2", frm="engaged.applicant@mail.example.net"), APPLY_BODY)  # summary
    _extract(_msg("m4", "c4", frm=COLLEAGUE.replace("pat", "kim"), subject="Lunch"), "Noon?")  # full
    get = ["get-message", "--message-id"]
    # cron: local refused before and after the read; the others pass as views
    assert mp.release_precheck("graph_actions", "get-message", get + ["m1"], "cron-morning-1") == mp.KEPT_REFUSAL
    assert mp.release_precheck("graph_actions", "get-message", get + ["m2"], "cron-morning-1") == ""
    assert mp.release_filter("graph_actions", "get-message", get + ["m1"], "cron-morning-1",
                             _read("m1", "c1")) == ("", mp.KEPT_REFUSAL)
    for run in ("cron-morning-1", "1a2b-c3d4", "cron-ghost-codex-x1"):
        out, refusal = mp.release_filter("graph_actions", "get-message", get + ["m2"], run, _read("m2", "c2"))
        row = json.loads(out)
        assert refusal == "" and row["kept_level"] == "Summary" and "RAW" not in out, run
        assert row["body"].startswith("A prospective student asks")
        out, _ = mp.release_filter("graph_actions", "get-message", get + ["m3"], run,
                                   _read("m3", "c3", frm=COLLEAGUE))
        row = json.loads(out)
        assert row["kept_level"] == "Facts only" and "body" not in row and "RAW" not in out
        assert "A prospective student" not in out and set(row) <= mp.FACT_FIELDS | {"_note"}
        raw = _read("m4", "c4", body="Noon?", frm="kim@partner.example.org")
        assert mp.release_filter("graph_actions", "get-message", get + ["m4"], run, raw) == (raw, ""), run
    # a chat turn reads local mail as its record, never the text
    out, refusal = mp.release_filter("graph_actions", "get-message", get + ["m1"], "1a2b-c3d4", _read("m1", "c1"))
    assert refusal == "" and json.loads(out)["kept_level"] == "Kept on your machine" and "RAW" not in out
    # an unread applicant message is judged from the read itself
    out, _ = mp.release_filter("graph_actions", "search-mail", [], "1a2b-c3d4", json.dumps(
        [{"id": "m-new", "subject": APPLY_SUBJECT, "from": APPLICANT, "conversation_id": "c9",
          "preview": "I am a prospective PhD student seeking PhD positions."}]))
    assert "seeking" not in out and json.loads(out)[0]["kept_level"] == "Kept on your machine"


def test_files_of_applicant_mail_stay_local_at_every_level_and_lane(env):
    _extract(_msg("m1", "c1"), APPLY_BODY)                                         # local
    pr.note_engaged({"engaged.applicant@mail.example.net": "2026-11-01T00:00:00Z"})
    pr.reset()
    _extract(_msg("m2", "c2", frm="engaged.applicant@mail.example.net"), APPLY_BODY)  # summary
    _rules({"text": "Treat this applicant as normal.", "match": "person:open.applicant@mail.example.net",
            "level": "full"})
    _extract(_msg("m5", "c5", frm="open.applicant@mail.example.net"), APPLY_BODY)   # full, still topic
    _extract(_msg("m4", "c4", frm="kim@partner.example.org", subject="Slides"), "The slides are attached.")
    for mid in ("m1", "m2", "m5"):
        for run in ("cron-x", "1a2b-c3d4", "cron-ghost-codex-y"):
            for tool, verb, argv in (("graph_actions", "download-attachment", ["--message-id", mid]),
                                     ("graph_actions", "scan-attachments", ["--message-id", mid]),
                                     ("mail_actions", "describe-attachment",
                                      ["describe-attachment", "--provider", "outlook", "--message-id", mid,
                                       "--all-images"])):
                assert mp.release_precheck(tool, verb, argv, run) == mp.FILES_REFUSAL, (mid, run, verb)
    assert mp.release_precheck("graph_actions", "download-attachment", ["--message-id", "m4"], "1a2b") == ""
    brief = mp.thread_brief("c2", user={ME})
    assert not brief["messages"][0].get("has_images")


# ── a hosted model never meets kept mail, on any lane ─────────────────

def test_the_tagger_never_sends_kept_mail_to_a_hosted_model(env, monkeypatch):
    import hosted_text
    import mail_tagger as mt
    hosted_prompts, local_prompts = [], []
    monkeypatch.setattr(mt, "_remote_mode", lambda: False)
    monkeypatch.setattr(hosted_text, "model_for", lambda task: "codex")
    monkeypatch.setattr(hosted_text, "codex", lambda prompt, **kw: hosted_prompts.append(prompt) or
                        {"rc": 0, "text": json.dumps({"tags": {"0": {"domain": "research", "state": "fyi"}}})})
    _extract(_msg("m1", "c1"), APPLY_BODY)
    applicant = dict(_msg("m1", "c1"), bodyPreview=APPLY_BODY[:200], categories=[])
    unread = dict(_msg("m7", "c7", frm="other.applicant@mail.example.net"), bodyPreview=APPLY_BODY, categories=[])
    plain = dict(_msg("m4", "c4", frm="kim@partner.example.org", subject="Lunch Tuesday"),
                 bodyPreview="Lunch at noon?", categories=[])
    state = {"recent": {}, "failures": {}, "senders": {}}

    def local(prompt):
        local_prompts.append(prompt)
        return {}, {}

    res = mt.tag_messages([applicant, unread, plain], graph=None, state=state, dry_run=True, now=NOW,
                          vocab=[], local_asker=local)
    assert len(hosted_prompts) == 1 and "Lunch Tuesday" in hosted_prompts[0]
    assert APPLY_SUBJECT not in hosted_prompts[0] and "seeking" not in hosted_prompts[0]
    assert len(local_prompts) == 1 and APPLY_SUBJECT in local_prompts[0]
    assert res["kept_local"] == 2
    # the same with Claude as the hosted model: the class decides, not the name
    monkeypatch.setattr(hosted_text, "model_for", lambda task: "claude")
    assert mt.kept_ids([applicant, unread, plain]) == {"m1", "m7"}


def test_a_codex_chat_turn_reads_as_a_claude_turn_and_is_counted_by_name(env):
    _extract(_msg("m1", "c1"), APPLY_BODY)
    _write_cfg(env, mail="sealed")
    raw = _read("m1", "c1")
    by_model = {}
    for model in ("claude:opus", "codex"):
        out, refusal = mp.release_filter("graph_actions", "get-message", ["--message-id", "m1"], "1a2b-c3d4", raw)
        by_model[model] = out
        mp.note_release("graph_actions", "get-message", ["--message-id", "m1"], "1a2b-c3d4", out, model=model)
    assert by_model["codex"] == by_model["claude:opus"] and "RAW" not in by_model["codex"]
    rel = rl.read(mp.RELEASES)
    assert [r["model"] for r in rel] == ["claude:opus", "codex"] and all(r["hosted"] for r in rel)
    assert all(r["levels"] == {"local": 1} for r in rel)
    mp.note_release("graph_actions", "get-message", ["--message-id", "m1"], "shadow-x", raw, model="local")
    assert rl.read(mp.RELEASES)[-1]["hosted"] is False
    m = mp.measure(with_mail=False)
    assert m["kept_reads_by_model"] == {"claude:opus": 1, "codex": 1}
    assert m["kept_by_reason"] == {"admissions": 1} and m["kept_by_level"]["local"] == 1
    text = mp.summary_text(m, {}, 1, cfg=rl.config())
    assert "Kept from frontier models: 1 (list 0, admissions 1; kept on your machine 1" in text
    assert "kept, by model: claude:opus 1, codex 1" in text


def test_a_codex_ghost_replay_is_a_cron_lane(env):
    _extract(_msg("m1", "c1"), APPLY_BODY)
    import codex_ghost
    rid = codex_ghost.RUN_PREFIX + "chat-1"
    assert mp.release_precheck("graph_actions", "get-conversation", ["--conversation-id", "c1"], rid) == mp.KEPT_REFUSAL


def test_plugins_the_hosted_digest_and_the_audit_get_no_kept_text(env, monkeypatch):
    import plugin_mail
    import open_claims_audit
    from preprocessor import preprocess as pp
    applicant = dict(_msg("m7", "c7"), bodyPreview=APPLY_BODY)
    assert plugin_mail._kept(applicant, rl.config(), {"body": {"contentType": "text", "content": APPLY_BODY}}) \
        == "admissions"
    assert plugin_mail._kept(dict(_msg("m4", "c4", frm="kim@partner.example.org", subject="Lunch"),
                                  bodyPreview="Noon?"), rl.config()) == ""
    _extract(_msg("m1", "c1"), APPLY_BODY)
    pr.note_engaged({"engaged.applicant@mail.example.net": "2026-11-01T00:00:00Z"})
    pr.reset()
    _extract(_msg("m2", "c2", frm="engaged.applicant@mail.example.net"), APPLY_BODY)
    data = {"digests": [{"from": APPLICANT, "name": "Morgan", "messages": [
        {"subject": APPLY_SUBJECT, "text": APPLY_BODY, "message_id": "m1", "conversation_id": "c1"}]},
        {"from": "engaged.applicant@mail.example.net", "name": "E", "messages": [
            {"subject": APPLY_SUBJECT, "text": APPLY_BODY, "message_id": "m2", "conversation_id": "c2"}]}]}
    out = pp.keep_admissions_digest(data, rl.config())
    a, b = out["digests"][0]["messages"][0], out["digests"][1]["messages"][0]
    assert a["text"] == "" and a["kept_level"] == "Kept on your machine"
    assert b["text"].startswith("A prospective student asks") and b["kept_level"] == "Summary"
    assert "seeking" not in json.dumps(out)
    rows = open_claims_audit.without_kept([
        {"id": "m1", "conversationId": "c1", "subject": APPLY_SUBJECT, "bodyPreview": APPLY_BODY,
         "from": {"emailAddress": {"address": APPLICANT}}},
        {"id": "m2", "conversationId": "c2", "subject": APPLY_SUBJECT, "bodyPreview": APPLY_BODY,
         "from": {"emailAddress": {"address": "engaged.applicant@mail.example.net"}}}])
    assert [r["id"] for r in rows] == ["m2"] and "seeking" not in json.dumps(rows)


def test_the_digest_says_where_the_mail_is_kept(env):
    _extract(_msg("m1", "c1", received=_iso(NOW - timedelta(hours=1))), APPLY_BODY)
    rows = mg.digest_rows(NOW - timedelta(hours=3), NOW)
    text = mg.render_digest(rows, NOW - timedelta(hours=3))
    assert "*Kept on your machine*" in text and "no hosted model saw these" in text
