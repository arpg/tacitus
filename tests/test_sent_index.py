"""sent_index: the user's sent mail indexed with local embeddings, and the
examples the writer gets from it. A fake embedder and fake pages stand in
for the endpoint and Graph; nothing reaches the network or the real
private dir."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import ea_written  # noqa: E402
import sent_index as si  # noqa: E402


def emb(texts):
    """A bag-of-words vector: texts that share words point the same way."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in t.lower().replace(si.QUERY_INSTRUCT.lower(), "").split():
            v[int(hashlib.md5(w.strip(".,?!").encode()).hexdigest(), 16) % 64] += 1
        out.append(v)
    return out


def msg(i, to, day, text, conv=None, cc=()):
    return {"id": f"m{i}", "conversationId": conv or f"c{i}",
            "sentDateTime": f"2024-01-{day:02d}T10:00:00Z", "subject": f"subject {i}",
            "toRecipients": [{"emailAddress": {"address": a}} for a in ([to] if isinstance(to, str) else to)],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
            "body": {"contentType": "text", "content": text}}


BUDGET = "Hi Alex, the budget for the field season is attached, let me know what you think. Kit"
REVIEW = "Hi Alex, can we move the review to Friday? I think that works better for everyone. Kit"
THANKS = "Hi Sam, thanks for the budget numbers, they look right to me and I will pass them on. Kit"
EA = ("Hi Sam, the lab can host the visit on the ninth. We need the mount finished "
      "before then, and two people for the outdoor runs. Kit")


@pytest.fixture
def con(tmp_path):
    c = si.connect(tmp_path / "index.db")
    yield c
    c.close()


def _load(con, msgs, matcher=None):
    return si.add(con, msgs, matcher=matcher or ea_written.Matcher([]), embedder=emb, model="fake")


def test_the_store_is_owner_only(tmp_path):
    si.connect(tmp_path / "x.db").close()
    assert oct((tmp_path / "x.db").stat().st_mode & 0o777) == "0o600"


def test_add_indexes_own_text_and_skips_what_is_no_model(con):
    r = _load(con, [msg(1, "alex@x.edu", 1, BUDGET),
                    msg(2, "alex@x.edu", 2, "ok"),                         # too short
                    msg(3, [f"p{i}@x.edu" for i in range(6)], 3, BUDGET),  # a broadcast
                    msg(4, "sam@x.edu", 4, THANKS, cc=["alex@x.edu"])])
    assert r == {"added": 2, "skipped": 2, "ea_written": 0}
    assert _load(con, [msg(1, "alex@x.edu", 1, BUDGET)])["added"] == 0   # already there
    row = con.execute("SELECT recipients, words, model FROM mail WHERE id = 'm4'").fetchone()
    assert row["recipients"] == '["sam@x.edu", "alex@x.edu"]' and row["model"] == "fake"


def test_mail_the_ea_wrote_is_indexed_but_never_an_example(con):
    matcher = ea_written.Matcher([{"ts": "2024-01-01T00:00:00Z", "conversation_id": "c9",
                                   "text": EA}])
    r = _load(con, [msg(9, "sam@x.edu", 2, EA, conv="c9"), msg(4, "sam@x.edu", 3, THANKS)],
              matcher)
    assert r["ea_written"] == 1
    got = si.examples("the lab visit on the ninth", "sam@x.edu", con=con, embedder=emb)
    assert [x["id"] for x in got] == ["m4"]


def test_examples_put_the_same_recipient_first_then_anyone(con):
    _load(con, [msg(1, "alex@x.edu", 1, BUDGET), msg(2, "alex@x.edu", 2, REVIEW),
                msg(4, "sam@x.edu", 4, THANKS)])
    got = si.examples("Send the budget numbers", "alex@x.edu", con=con, embedder=emb,
                      k_same=1, k_any=1)
    assert [x["id"] for x in got] == ["m1", "m4"]
    assert got[0]["recipients"] == ["alex@x.edu"] and got[0]["score"] > 0


def test_examples_leave_out_the_conversation_later_mail_and_named_ids(con):
    _load(con, [msg(1, "alex@x.edu", 1, BUDGET), msg(2, "alex@x.edu", 2, REVIEW),
                msg(4, "sam@x.edu", 4, THANKS)])
    ids = lambda **kw: {x["id"] for x in si.examples("budget", "alex@x.edu", con=con,
                                                     embedder=emb, **kw)}
    assert ids(conversation_id="c1") == {"m2", "m4"}
    assert ids(before="2024-01-02T00:00:00Z") == {"m1"}
    assert ids(exclude_ids=("m1", "m4")) == {"m2"}


def test_near_duplicates_count_once(con):
    _load(con, [msg(1, "alex@x.edu", 1, BUDGET), msg(5, "sam@x.edu", 5, BUDGET.replace("Alex", "Sam"))])
    got = si.examples("budget", "", con=con, embedder=emb, k_any=3)
    assert len(got) == 1


def test_an_empty_store_never_calls_the_endpoint(con):
    def boom(texts):
        raise AssertionError("embedded with nothing to search")
    assert si.examples("anything", "alex@x.edu", con=con, embedder=boom) == []


def test_render_names_the_emails_and_cuts_long_ones():
    long = {"id": "m1", "recipients": ["alex@x.edu"], "sent_at": "2024-01-01T10:00:00Z",
            "text": "word " * 1000}
    out = si.render([long])
    assert out.startswith("=== The user's own emails") and "take no facts" in out
    assert "--- Email 1 (to alex@x.edu, 2024-01-01) ---" in out and out.endswith("[...]")
    assert si.render([]) == ""


def test_sync_pages_forward_and_resumes_from_the_watermark(con):
    pages = [{"value": [msg(1, "alex@x.edu", 1, BUDGET)], "@odata.nextLink": "next-1"},
             {"value": [msg(2, "alex@x.edu", 2, REVIEW)], "@odata.nextLink": "next-2"},
             RuntimeError("504")]
    seen = []

    def fetch(url, params):
        seen.append((url, params))
        page = pages[len(seen) - 1]
        if isinstance(page, Exception):
            raise page
        return page
    r = si.sync(con=con, fetch=fetch, matcher=ea_written.Matcher([]), embedder=emb)
    assert r["pages"] == 2 and r["added"] == 2 and r["stopped"].startswith("RuntimeError")
    assert r["watermark"] == "2024-01-02T10:00:00Z"
    assert seen[0][1]["$filter"] == f"sentDateTime ge {si.FIRST_SINCE}T00:00:00Z"
    assert seen[1] == ("next-1", None)
    seen.clear()
    pages[:] = [{"value": [msg(2, "alex@x.edu", 2, REVIEW), msg(4, "sam@x.edu", 4, THANKS)]}]
    r = si.sync(con=con, fetch=fetch, matcher=ea_written.Matcher([]), embedder=emb)
    assert seen[0][1]["$filter"] == "sentDateTime ge 2024-01-02T10:00:00Z"
    assert r["added"] == 1 and "stopped" not in r
    assert si.stats(con)["messages"] == 3


def test_an_embedding_failure_writes_nothing_of_the_page(con):
    def down(texts):
        raise si.EmbedError("connection refused")
    with pytest.raises(si.EmbedError):
        si.add(con, [msg(1, "alex@x.edu", 1, BUDGET)], matcher=ea_written.Matcher([]),
               embedder=down)
    assert si.stats(con)["messages"] == 0


def test_embed_batches_and_checks_the_answer():
    calls = []

    class R:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": i, "embedding": [float(i)]} for i in reversed(range(self.n))]}

    def post(url, json, timeout):
        calls.append((url, json["model"], len(json["input"])))
        return R(len(json["input"]))
    out = si.embed(["t"] * 40, endpoint="http://127.0.0.1:11434/v1", model="m", post=post)
    assert len(out) == 40 and out[0] == [0.0] and out[1] == [1.0]
    assert calls == [("http://127.0.0.1:11434/v1/embeddings", "m", 32),
                     ("http://127.0.0.1:11434/v1/embeddings", "m", 8)]

    def short(url, json, timeout):
        return R(1)
    with pytest.raises(si.EmbedError):
        si.embed(["a", "b"], endpoint="http://e/v1", model="m", post=short)


def test_embed_config_reads_the_private_config():
    assert si.embed_config({}) == (si.EMBED_ENDPOINT, si.EMBED_MODEL)
    assert si.embed_config({"embed_endpoint": "http://box:8000/v1/", "embed_model": "e5"}) == (
        "http://box:8000/v1", "e5")


def test_add_fetched_reports_rather_than_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "db_path", lambda: tmp_path / "i.db")
    monkeypatch.setattr(si, "embed", lambda texts, **kw: (_ for _ in ()).throw(si.EmbedError("down")))
    monkeypatch.setattr(ea_written, "load", lambda folder=None: ea_written.Matcher([]))
    out = si.add_fetched([msg(1, "alex@x.edu", 1, BUDGET)])
    assert "down" in out["error"]


def test_mail_from_the_unchecked_window_is_never_an_example(con, monkeypatch):
    _load(con, [msg(1, "alex@x.edu", 1, BUDGET), msg(2, "alex@x.edu", 5, REVIEW),
                msg(4, "alex@x.edu", 20, THANKS.replace("Sam", "Alex"))])
    monkeypatch.setattr(si, "ea_since", lambda cfg=None: "2024-01-03T00:00:00Z")
    assert si.unchecked(con) == ("", "")          # no first sync yet: no window
    si._state(con, "checked_from", "2024-01-10T00:00:00Z")
    assert si.unchecked(con) == ("2024-01-03T00:00:00Z", "2024-01-10T00:00:00Z")
    ids = {x["id"] for x in si.examples("budget", "alex@x.edu", con=con, embedder=emb, k_any=0)}
    assert ids == {"m1", "m4"}
    assert si.stats(con)["unchecked"] == {"from": "2024-01-03T00:00:00Z",
                                          "until": "2024-01-10T00:00:00Z"}


def test_the_first_sync_marks_where_the_records_begin(con):
    si.sync(con=con, fetch=lambda url, params: {"value": []},
            matcher=ea_written.Matcher([]), embedder=emb)
    first = si._state(con, "checked_from")
    assert first.endswith("Z")
    si.sync(con=con, fetch=lambda url, params: {"value": []},
            matcher=ea_written.Matcher([]), embedder=emb)
    assert si._state(con, "checked_from") == first


def test_ea_since_prefers_the_config_and_falls_back_to_the_ledger(monkeypatch, tmp_path):
    import sqlite3
    import ledger
    assert si.ea_since({"ea_drafting_since": "2026-04-01"}) == "2026-04-01"
    db = tmp_path / "ledger.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ledger (created_at TEXT)")
    c.executemany("INSERT INTO ledger VALUES (?)", [("2026-05-01T00:00:00Z",), ("2026-04-02T00:00:00Z",)])
    c.commit()
    c.close()
    monkeypatch.setattr(ledger, "DB_PATH", db)
    assert si.ea_since({}) == "2026-04-02T00:00:00Z"
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "missing.db")
    assert si.ea_since({}) == ""
