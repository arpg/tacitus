"""remote_digests: the switch (the Claude lane runs while it is off), the
list-digest judgment and post on a fake model, post-then-archive and its
failure paths, link following under fetch_url's address policy, and the
weekly admissions post. All mail here is made up. Nothing reaches the
network, Graph, the chat or the real private dir."""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import remote_digests as rd  # noqa: E402

TODAY = date(2026, 9, 1)


def _write_cfg(d, **kw):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off",
           "scheduling": "off", "shadow_until": "2099-01-01", **kw}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    _write_cfg(d, digests="remote")
    return d


class Model:
    """A scripted remote model: each call takes the next answer (an
    exception is raised)."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return json.loads(json.dumps(a)), {"latency_ms": 10}


class Sink:
    """Records calls in a shared event list; returns `result`."""

    def __init__(self, events, name, result=True):
        self.events, self.name, self.result, self.calls = events, name, result, []

    def __call__(self, *args):
        self.calls.append(args)
        self.events.append(self.name)
        return self.result


def _filter_entry():
    return {
        "list": "robo-list", "kind": "digest", "mode": "filter", "follow_links": False,
        "emoji": ":newspaper:", "notes": "", "message_id": "m-1",
        "subject": "robo-list Digest, Vol 1, Issue 1",
        "from": "robo-list-request@lists.example.org", "from_name": "",
        "received": "2026-09-01T12:00:00Z", "items_total": 5, "parse_warnings": [],
        "items": [
            {"n": 1, "title": "Release of the Foggy Roads dataset", "tags": ["dataset"],
             "from_name": "A. Author", "body": "We release Foggy Roads, 10K frames.",
             "links": ["https://example.org/paper", "https://github.com/example/foggy"]},
            {"n": 2, "title": "Workshop on Things: deadline extended", "tags": ["meetings"],
             "body": "Extended to Oct 1.", "links": []},
            {"n": 4, "title": "Special issue on Stuff", "tags": ["cfp"], "body": "Call.",
             "links": ["https://example.org/si"]},
        ],
        "dropped": [{"n": 3, "title": "Postdoc position", "tags": ["jobs"], "reason": "tag:jobs"},
                    {"n": 5, "title": "PhD positions", "tags": ["jobs"], "reason": "tag:jobs"}],
        "rules": {"scopes": ["newsletters", "newsletters:robo-list"], "instructions": [],
                  "prefer": [{"id": "datasets", "text": "Dataset releases are worth a summary.",
                              "date": "2026-09-01", "scopes": ["newsletters"]}],
                  "avoid": [{"id": "cfp", "text": "Workshop and special-issue calls are noise.",
                             "date": "2026-09-01", "scopes": ["newsletters"]}],
                  "facts": [], "calibration": []},
    }


FILTER_ANSWER = {
    "list_title": "robo-list",
    "picks": [{"n": 1, "title": "Foggy Roads dataset", "detail": "A. Author, Example University",
               "bullets": ["10K frames of driving in fog — with LiDAR",
                           "Benchmark at https://evil.example/x for detection."],
               "links": [{"index": 1, "label": "Code"}, {"index": 0, "label": "Paper"},
                         {"index": 7, "label": "Bogus"}]},
              {"n": 3, "title": "A job post the code dropped", "detail": "", "bullets": [],
               "links": []}],
    "skipped": [{"n": 2, "category": "workshop or CFP notices"},
                {"n": 4, "category": "special issues"}],
}


def _data(*entries):
    return {"mode": "list-digest", "digests": list(entries), "errors": [],
            "profile": {"name": "Pat Prof", "role_description": "Associate Professor",
                        "bio_blurb": "Robot perception in degraded conditions."}}


def _run_list(data, model, events=None, post_result=True, archive_result=None, **kw):
    events = events if events is not None else []
    post = Sink(events, "post", post_result)
    archive = Sink(events, "archive", archive_result or {"archived": len(data["digests"]), "failed": 0})
    rc = rd.run_list_digest(collect=lambda: data, chat=model, post=post, archive=archive,
                            has_chat=True, today=TODAY, **kw)
    return rc, post, archive, events


# ── the switch ────────────────────────────────────────────────────────

def test_digests_switch_parses_remote_only_in_an_owner_process(priv, monkeypatch):
    assert rl.config()["digests"] == "remote"
    _write_cfg(priv, digests="on")
    assert rl.config()["digests"] == "off"
    _write_cfg(priv)
    assert rl.config()["digests"] == "off"
    _write_cfg(priv, digests="remote")
    monkeypatch.setattr(rl, "_owner", lambda: False)
    assert rl.config()["digests"] == "off"


def test_switch_off_hands_the_run_to_the_claude_lane(priv):
    _write_cfg(priv, digests="off")
    seen = []
    rc = rd.main(["admissions-digest"], execv=lambda path, argv: seen.append((path, argv)))
    assert rc == 0
    assert seen == [(str(rd.RUN_AGENT), [str(rd.RUN_AGENT), "admissions-digest"])]
    assert rd.RUN_AGENT == Path(__file__).resolve().parents[1] / "run-agent.sh"
    assert rd.RUN_AGENT.exists()


def test_switch_on_runs_the_remote_lane_and_never_the_claude_one(priv, monkeypatch):
    import ea_config
    monkeypatch.setattr(ea_config, "is_ea_enabled", lambda *a: True)
    monkeypatch.setattr(rd._owner_trust, "is_owner_process", lambda: True)
    ran = []
    monkeypatch.setattr(rd, "run_list_digest", lambda **kw: ran.append(kw) or 0)
    rc = rd.main(["list-digest"], execv=lambda *a: pytest.fail("the Claude lane ran"))
    assert rc == 0 and ran and ran[0]["dry_run"] is False and ran[0]["cfg"]["digests"] == "remote"


# ── list digests ──────────────────────────────────────────────────────

def test_filter_list_posts_the_skill_shape_then_archives(priv):
    data = _data(_filter_entry())
    model = Model(FILTER_ANSWER)
    rc, post, archive, events = _run_list(data, model)
    assert rc == 0
    assert events == ["post", "archive"]
    channel, text = post.calls[0]
    assert channel == "list-digest"
    assert text.splitlines()[0] == ":newspaper: *robo-list digest* (Tue 01 Sep): 1 of 5 items is relevant."
    assert "*Foggy Roads dataset* (A. Author, Example University)" in text
    assert "• 10K frames of driving in fog, with LiDAR." in text
    assert "—" not in text
    assert "evil.example" not in text and "Benchmark" not in text  # a bullet with a typed URL goes
    assert "Code: https://github.com/example/foggy | Paper: https://example.org/paper" in text
    assert "job post the code dropped" not in text          # n 3 is not a candidate
    assert text.splitlines()[-1] == ("Dropped 4: 2 job posts, 1 special issues, "
                                     "1 workshop or CFP notices.")
    assert archive.calls == [(data, "posted")]
    call = model.calls[0]
    assert call["purpose"] == "digest-list" and call["schema"] is rd.FILTER_SCHEMA
    system = call["messages"][0]["content"]
    assert "Dataset releases are worth a summary." in system
    assert "Workshop and special-issue calls are noise." in system
    assert "Pat Prof" in system
    rec = rl.read(rd.RUNS)[-1]
    assert rec["disposition"] == "posted" and rec["posted"] is True and rec["post"] == text


def test_nothing_relevant_archives_silently_with_no_post(priv):
    answer = {"list_title": "robo-list", "picks": [],
              "skipped": [{"n": 1, "category": "out-of-field posts"}]}
    rc, post, archive, events = _run_list(_data(_filter_entry()), Model(answer))
    assert rc == 0 and post.calls == []
    assert [c[1] for c in archive.calls] == ["silent"]


def test_endpoint_failure_posts_nothing_and_archives_nothing(priv, capsys):
    rc, post, archive, _ = _run_list(_data(_filter_entry()), Model(rl.RemoteLLMError("HTTP 502")))
    assert rc == 1 and post.calls == [] and archive.calls == []
    assert "the mail stays for the next run" in capsys.readouterr().err
    assert rl.read(rd.RUNS)[-1]["ok"] is False


def test_failed_post_archives_nothing(priv):
    rc, post, archive, _ = _run_list(_data(_filter_entry()), Model(FILTER_ANSWER), post_result=False)
    assert rc == 1 and len(post.calls) == 1 and archive.calls == []


def test_failed_archive_warns_in_the_channel(priv):
    rc, post, archive, _ = _run_list(_data(_filter_entry()), Model(FILTER_ANSWER),
                                     archive_result={"archived": 0, "failed": 1})
    assert rc == 1 and len(post.calls) == 2
    assert post.calls[1][1].startswith(":warning: list-digest: a digest was judged but could not be archived")


def test_no_digest_in_the_inbox_asks_nothing(priv):
    model = Model()
    rc, post, archive, _ = _run_list(_data(), model)
    assert rc == 0 and model.calls == [] and post.calls == [] and archive.calls == []


def test_without_chat_the_report_file_is_the_delivery(priv):
    data = _data(_filter_entry())
    events, written = [], []
    rc = rd.run_list_digest(collect=lambda: data, chat=Model(FILTER_ANSWER),
                            post=Sink(events, "post"),
                            archive=Sink(events, "archive", {"archived": 1, "failed": 0}),
                            has_chat=False, write_report=lambda a, t: written.append((a, t)) or True,
                            today=TODAY)
    assert rc == 0 and events == ["archive"]
    assert written and written[0][0] == "list-digest" and written[0][1].startswith(":newspaper:")


def test_dry_run_prints_counts_only_and_changes_nothing(priv, capsys):
    rc, post, archive, _ = _run_list(_data(_filter_entry()), Model(FILTER_ANSWER), dry_run=True)
    assert rc == 0 and post.calls == [] and archive.calls == []
    out = capsys.readouterr().out
    counts = json.loads(out)
    assert counts["dry_run"] is True and counts["picked"] == 1 and counts["items"] == 5
    assert "Foggy" not in out                              # counts, never content
    rec = rl.read(rd.RUNS)[-1]
    assert rec["dry_run"] is True and "Foggy Roads" in rec["post"]


def _prioritize_entry():
    items = [
        {"n": 1, "title": "Seed Grant information session", "section": "Events", "deadline": "10/12",
         "body": "10/12: Seed Grant information session", "links": ["https://example.edu/seed-info"]},
        {"n": 2, "title": "Research Seed Grant", "section": "Internal", "deadline": "9/15",
         "stage": "", "is_new": True, "body": "Due 9/15: Research Seed Grant",
         "links": ["https://example.edu/seed"]},
        {"n": 3, "title": "Rolling Travel Fund", "deadline": "rolling", "body": "Rolling",
         "links": ["https://example.edu/travel"]},
        {"n": 4, "title": "Instrumentation Program", "deadline": "11/04", "body": "11/04",
         "links": ["https://example.gov/instr"]},
        {"n": 5, "title": "Marine Biology Award", "deadline": "9/20", "body": "9/20",
         "links": ["https://example.org/marine"]},
    ]
    return {"list": "seed-bulletin", "kind": "newsletter", "mode": "prioritize",
            "follow_links": True, "emoji": ":moneybag:",
            "notes": "Limited Submission due dates are internal.", "message_id": "m-2",
            "subject": "Seed Bulletin", "from": "office@example.edu", "from_name": "Office",
            "received": "2026-09-01T12:00:00Z", "items_total": 5, "parse_warnings": [],
            "items": items, "dropped": [],
            "rules": {"scopes": ["newsletters", "newsletters:seed-bulletin", "funding"],
                      "instructions": [], "prefer": [], "avoid": [], "facts": [],
                      "calibration": []}}


def test_prioritize_reads_pages_nearest_deadline_first_within_the_cap(priv, monkeypatch):
    monkeypatch.setattr(rd, "MAX_FETCH", 2)
    screen = {"list_title": "Seed Bulletin", "check": [1, 2, 3, 4, 99],
              "skipped": [{"n": 5, "category": "out-of-field programs"}]}
    rank = {"list_title": "Seed Bulletin",
            "picks": [{"n": 2, "title": "Research Seed Grant", "detail": "due 9/15; internal",
                       "bullets": ["Up to $60K over 18 months.", "Two or more faculty PIs"],
                       "links": [{"index": 0, "label": ""}]},
                      {"n": 4, "title": "Not a candidate: never read", "detail": "", "bullets": [],
                       "links": []}],
            "radar": [{"n": 1, "note": "Seed Grant information session (10/12)"}],
            "skipped": []}
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.endswith("/seed"):
            return {"url": url, "title": "Seed", "text": "The program funds up to $60K."}
        return {"url": url, "error": "HTTP 404"}

    model = Model(screen, rank)
    data = _data(_prioritize_entry())
    rc, post, archive, _ = _run_list(data, model, fetch=fetch)
    assert rc == 0
    assert fetched == ["https://example.edu/seed", "https://example.edu/seed-info"]
    assert [c["schema"] for c in model.calls] == [rd.SCREEN_SCHEMA, rd.RANK_SCHEMA]
    rank_prompt = model.calls[1]["messages"][1]["content"]
    assert "The program funds up to $60K." in rank_prompt
    assert "page: could not be read (HTTP 404)" in rank_prompt
    assert "Instrumentation Program" not in rank_prompt     # past the fetch cap
    text = post.calls[0][1]
    lines = text.splitlines()
    assert lines[0] == ":moneybag: *Seed Bulletin* (Tue 01 Sep): 1 of 5 items is worth your time."
    assert "*1. Research Seed Grant* (due 9/15; internal)" in text
    assert "• Two or more faculty PIs." in text
    assert "https://example.edu/seed" in text
    assert "Not a candidate" not in text
    assert "Also on the radar: Seed Grant information session (10/12)." in text
    assert "Not checked: Instrumentation Program; Rolling Travel Fund." in text
    assert lines[-1] == "Skipped 4: 2 not checked, 1 event, 1 out-of-field programs."
    assert archive.calls == [(data, "posted")]
    rec = rl.read(rd.RUNS)[-1]
    assert rec["fetched"] == 2 and rec["fetch_failed"] == 1
    assert {f["host"] for f in rec["fetches"]} == {"example.edu"}


def test_deadline_order():
    keys = {d: rd.deadline_key(d, TODAY) for d in ("9/15", "10/12", "1/10", "8/20", "rolling", "",
                                                   "10/1 & 10/8", "9/30/2026")}
    assert keys["9/15"] < keys["9/30/2026"] < keys["10/1 & 10/8"] < keys["10/12"] < keys["1/10"]
    assert keys["1/10"] == date(2027, 1, 10)
    assert keys["8/20"] == keys["rolling"] == keys[""] == date.max


# ── link following ────────────────────────────────────────────────────

class Resp:
    def __init__(self, status=200, headers=None, body=b"", encoding="utf-8"):
        self.status_code, self.headers, self.body, self.encoding = status, headers or {}, body, encoding
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield self.body

    def close(self):
        self.closed = True


def test_fetch_refuses_internal_addresses_without_a_request():
    def get(*a, **kw):
        pytest.fail("a request was made")
    assert rd.fetch_page("http://127.0.0.1/admin", get=get)["error"].startswith("blocked")
    assert rd.fetch_page("file:///etc/passwd", get=get)["error"].startswith("blocked")
    assert rd.fetch_page("https://example.org/call.pdf", get=get)["error"] == "a PDF"


def test_fetch_checks_every_redirect_hop():
    asked = []

    def get(url, **kw):
        asked.append(url)
        assert kw["allow_redirects"] is False
        return Resp(302, {"Location": "http://10.1.2.3/internal"})
    out = rd.fetch_page("http://93.184.216.34/start", get=get)
    assert asked == ["http://93.184.216.34/start"]
    assert out["error"].startswith("blocked")


def test_fetch_returns_the_page_text_capped():
    html = ("<html><head><title>Seed Grant</title></head><body><main><p>"
            + "The program funds early work. " * 40 + "</p></main></body></html>").encode()

    def get(url, **kw):
        return Resp(200, {"content-type": "text/html; charset=utf-8"}, html)
    out = rd.fetch_page("http://93.184.216.34/seed", get=get, max_chars=200)
    assert out["title"] == "Seed Grant"
    assert out["text"].startswith("The program funds early work.") and len(out["text"]) <= 200


# ── admissions digest ─────────────────────────────────────────────────

def _admissions_data():
    def msg(subject, text, received="2026-10-01T10:00:00Z"):
        return {"subject": subject, "received": received, "has_attachments": False,
                "kinds": ["subject:prospective-student"], "text": text,
                "message_id": "x", "conversation_id": "c"}
    return {"mode": "admissions-digest", "window_days": 7, "messages": 4, "senders": 3,
            "errors": ["'x': body fetch failed: timeout"],
            "profile": {"name": "Pat Prof", "bio_blurb": "Robot perception."},
            "digests": [
                {"from": "dov.stavros9@example.com", "name": "Ana Example", "domain": "uni-a.example",
                 "count": 1, "follow_ups": 0,
                 "messages": [msg("Prospective PhD student, Fall 2027", "I built a radar odometry stack.")]},
                {"from": "ira.corvino53@example.com", "name": "Bo Sample", "domain": "uni-b.example",
                 "count": 2, "follow_ups": 1,
                 "messages": [msg("PhD inquiry", "Dear Professor, I admire your lab."),
                              msg("Re: PhD inquiry", "Following up.")]},
                {"from": "harper.yarborough46@example.com", "name": "", "domain": "uni-c.example",
                 "count": 1, "follow_ups": 0,
                 "messages": [msg("Seeking PhD positions", "I found a gap in robustness.")]},
            ]}


ADMISSIONS_ANSWER = {"senders": [
    {"i": 1, "shortlist": True, "strength": 4, "school": "Uni A",
     "subject": "Prospective PhD student, Fall 2027",
     "fit": "built a radar odometry stack — close to the lab's work", "pattern": "real-fit"},
    {"i": 2, "shortlist": False, "strength": 1, "school": "", "subject": "PhD inquiry",
     "fit": "", "pattern": "templated"},
    {"i": 3, "shortlist": False, "strength": 2, "school": "", "subject": "", "fit": "",
     "pattern": "generic-gap"},
    {"i": 9, "shortlist": True, "strength": 5, "school": "", "subject": "", "fit": "", "pattern": "other"},
]}

PREFS = {"rules": [
    {"id": "mass", "kind": "avoid", "scopes": ["admissions"], "text": "Never shortlist mass mailings.",
     "status": "active", "date": "2026-10-01"},
    {"id": "news", "kind": "avoid", "scopes": ["newsletters"], "text": "A newsletter rule.",
     "status": "active", "date": "2026-10-01"}],
    "calibration": []}


def _run_admissions(model, data=None, **kw):
    events = []
    post = Sink(events, "post")
    rc = rd.run_admissions_digest(collect=lambda: data or _admissions_data(), chat=model, post=post,
                                  enabled=lambda: True, has_chat=True, prefs_data=PREFS,
                                  now=datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc), **kw)
    return rc, post


def test_admissions_post_has_the_skill_shape(priv):
    model = Model(ADMISSIONS_ANSWER)
    rc, post = _run_admissions(model)
    assert rc == 0
    channel, text = post.calls[0]
    assert channel == "admissions-digest"
    assert text.splitlines() == [
        ":mortar_board: *Admissions inquiries: week of Mon 28 Sep*",
        "4 emails from 3 senders went to the admissions-inquiries folder.",
        "",
        "*Worth a look* (1)",
        "- *Ana Example* (Uni A): Prospective PhD student, Fall 2027. "
        "Built a radar odometry stack, close to the lab's work.",
        "",
        "*The rest* (2): Bo Sample, harper.yarborough46@example.com. "
        "1 generic research-gap claims, 1 templated asks; 1 sent follow-ups.",
        "",
        ":warning: 1 message(s) could not be read in full.",
    ]
    call = model.calls[0]
    assert call["purpose"] == "digest-admissions" and call["schema"] is rd.ADMISSIONS_SCHEMA
    system = call["messages"][0]["content"]
    assert "Never shortlist mass mailings." in system
    assert "A newsletter rule." not in system
    assert "I built a radar odometry stack." in call["messages"][1]["content"]
    assert rl.read(rd.RUNS)[-1]["shortlisted"] == 1


def test_admissions_batches_senders_by_size(priv, monkeypatch):
    monkeypatch.setattr(rd, "ADMISSIONS_BATCH_CHARS", 10)
    answers = [{"senders": [a]} for a in ADMISSIONS_ANSWER["senders"][:3]]
    model = Model(*answers)
    rc, post = _run_admissions(model)
    assert rc == 0 and len(model.calls) == 3
    assert "*Worth a look* (1)" in post.calls[0][1]


def test_admissions_endpoint_failure_posts_nothing(priv):
    rc, post = _run_admissions(Model(rl.RemoteLLMError("circuit open after repeated failures")))
    assert rc == 1 and post.calls == []


def test_admissions_off_for_the_install_reads_nothing(priv):
    rc = rd.run_admissions_digest(collect=lambda: pytest.fail("collected"), chat=Model(),
                                  enabled=lambda: False)
    assert rc == 0


def test_admissions_unreadable_folder_posts_nothing(priv):
    model = Model()
    rc, post = _run_admissions(model, data={"digests": [], "errors": ["folder read failed: x"]})
    assert rc == 1 and post.calls == [] and model.calls == []


def test_admissions_empty_week_is_quiet(priv):
    model = Model()
    rc, post = _run_admissions(model, data={"digests": [], "errors": []})
    assert rc == 0 and post.calls == [] and model.calls == []


def test_unreadable_mail_posts_nothing(priv, capsys):
    def collect():
        raise RuntimeError("Graph 503")
    events = []
    rc = rd.run_list_digest(collect=collect, chat=Model(), post=Sink(events, "post"),
                            archive=Sink(events, "archive"), has_chat=True)
    assert rc == 1 and events == []
    assert "the mail could not be read" in capsys.readouterr().err


def test_on_zulip_the_post_goes_to_the_mail_digest_stream(monkeypatch):
    import chat_notify
    sent = []
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "zulip")
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: {"zulip": {"site": "s"}})
    monkeypatch.setattr(chat_notify, "_zulip_post_parts",
                        lambda zcfg, target, parts: sent.append((target, parts)) or True)
    assert rd._post("list-digest", "digest text")
    assert sent == [({"stream": chat_notify.DIGEST_STREAM, "topic": rd.DIGEST_TOPIC},
                     ["digest text"])]


def test_on_slack_the_post_takes_the_digest_webhook_else_the_ea_channel(monkeypatch):
    import chat_notify
    import slack_notifier
    hooks, slack, ea = {}, [], []
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: hooks)
    monkeypatch.setattr(chat_notify, "_send_slack",
                        lambda key, parts, blocks: slack.append((key, parts)) or True)
    monkeypatch.setattr(slack_notifier, "post_to_slack",
                        lambda key, text: ea.append((key, text)) or True)
    assert rd._post("admissions-digest", "week text")
    assert ea == [(rd.CHANNEL, "week text")] and slack == []
    hooks["mail-digest"] = "https://hooks.example/D"
    assert rd._post("admissions-digest", "week text")
    assert slack == [("mail-digest", ["week text"])] and len(ea) == 1
