"""voice_eval: held-out briefs, one draft per arm, the judge, the blind
page and the summary. Fake chat and embeddings; nothing reaches the
network or the real private dir."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import draft_ab  # noqa: E402
import ea_written  # noqa: E402
import remote_llm as rl  # noqa: E402
import sent_index as si  # noqa: E402
import voice_eval as ve  # noqa: E402
from test_sent_index import emb, msg  # noqa: E402

TEXTS = [
    "Hi Alex, the budget for the field season is attached. Can you check the travel line "
    "before Friday? I think we are close but the per diem looks low to me. The rest can "
    "wait until the review next month. Kit",
    "Hi Sam, thanks for the numbers. They look right to me and I will pass them to the "
    "office today. Can we talk Monday about the second year? The renewal is due soon and "
    "I want us ready. Kit",
    "Hi Jo, sorry for the slow reply! The room is booked for Tuesday at two and I will "
    "bring the sensors. Let me know if the cart is free that day, otherwise we can carry "
    "everything down by hand. Kit",
    "Hi Lee, I can review the paper but not before the tenth. If that works for the "
    "editor I am happy to take it on. Otherwise please ask someone else, and thanks for "
    "thinking of me. Kit",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(draft_ab, "_style_profile", lambda addr: json.dumps({"found": False}))
    con = si.connect(tmp_path / "index.db")
    to = ["alex@x.edu", "sam@x.edu", "jo@x.edu", "lee@x.edu"]
    si.add(con, [msg(i + 1, to[i], i + 1, t) for i, t in enumerate(TEXTS)],
           matcher=ea_written.Matcher([]), embedder=emb)
    yield con
    con.close()


class Chat:
    """Answers the brief, writer and judge calls; records each."""

    def __init__(self, pick="A"):
        self.calls, self.pick = [], pick

    def __call__(self, messages, **kw):
        self.calls.append((kw.get("purpose"), messages))
        purpose = kw.get("purpose")
        if purpose == "voice-eval-brief":
            text = json.dumps({"recipient_name": "Alex", "say": "Ask them to check the numbers by Friday.",
                               "kind": "request"})
        elif purpose == "voice-eval-judge":
            text = json.dumps({"pick": self.pick})
        else:
            text = "Hi there,\n\nPlease check the numbers by Friday.\n\nKit"
        return {"content": text, "contents": [text], "latency_ms": 5}


def test_build_back_translates_held_out_mail(env):
    chat = Chat()
    r = ve.build(3, "2024-01-01", "2024-02-01", con=env, chat=chat)
    assert r["added"] == 3 and r["failed"] == 0
    items = rl.read(ve.ITEMS)
    assert len(items) == 3 and all(x["brief"].startswith("Recipient: ") for x in items)
    assert all("(Alex)." in x["brief"] for x in items)
    assert all("SAY: Ask them" in x["brief"] and x["real"] in TEXTS for x in items)
    sys_prompt, user = chat.calls[0][1][0]["content"], chat.calls[0][1][1]["content"]
    assert "Do not copy phrases" in sys_prompt and "Email:\n" in user
    # A second build tops up to the target and never repeats an item.
    r = ve.build(4, "2024-01-01", "2024-02-01", con=env, chat=chat)
    assert r["added"] == 1 and len({x["id"] for x in rl.read(ve.ITEMS)}) == 4


def test_generate_drafts_each_arm_as_of_the_item(env):
    chat = Chat()
    ve.build(4, "2024-01-01", "2024-02-01", con=env, chat=chat)
    r = ve.generate(list(ve.ARMS), chat=chat, con=env, embedder=emb)
    assert r == {"made": 24, "failed": 0}
    have = ve.bodies()
    assert len(have) == 24
    items = {x["id"]: x for x in rl.read(ve.ITEMS)}
    for (iid, arm), b in have.items():
        item = items[iid]
        if arm == "baseline":
            assert b["examples"] == []
        else:
            # Only mail sent before the item, never the item itself.
            sent = {f"m{i + 1}": f"2024-01-{i + 1:02d}T10:00:00Z" for i in range(4)}
            assert iid not in b["examples"]
            assert all(sent[e] < item["sent_at"] for e in b["examples"])
    writer = [m for p, m in chat.calls if p == "voice-eval"]
    with_ex = [m for m in writer if "The user's own emails" in m[0]["content"]]
    counted = [m for m in with_ex if "Across these" in m[0]["content"]]
    assert with_ex and len(with_ex) < len(writer) and 0 < len(counted) < len(with_ex)
    assert ve.generate(list(ve.ARMS), chat=chat, con=env, embedder=emb) == {"made": 0, "failed": 0}


def test_judge_asks_both_orders_and_scores_the_real_side(env):
    chat = Chat(pick="A")
    ve.build(2, "2024-01-01", "2024-02-01", con=env, chat=chat)
    ve.generate(["baseline"], chat=chat, con=env, embedder=emb)
    assert ve.judge(["baseline"], chat=chat, con=env) == {"judged": 4}
    js = rl.read(ve.JUDGED)
    # Always answering A finds the real email in one order of the two.
    assert sorted(j["correct"] for j in js) == [False, False, True, True]
    assert ve.judge(["baseline"], chat=chat, con=env) == {"judged": 0}
    s = ve.summary()
    assert s["arms"]["baseline"]["judge_found_real"] == 0.5
    assert s["arms"]["baseline"]["judge_calls"] == 4
    assert s["arms"]["baseline"]["paired_items"] == 2


def test_each_item_is_voted_in_one_arm_and_the_vote_is_scored(env):
    chat = Chat()
    ve.build(4, "2024-01-01", "2024-02-01", con=env, chat=chat)
    ve.generate(list(ve.ARMS), chat=chat, con=env, embedder=emb)
    pairs = ve.assignment(rl.read(ve.ITEMS), ve.bodies())
    assert len(pairs) == 4 and {a for _, a in pairs} == set(ve.PAGE_ARMS)
    assert len({item["id"] for item, _ in pairs}) == 4
    item, arm = pairs[0]
    real = ve.sides(item, arm)
    other = "2" if real == "1" else "1"
    assert ve.record(item, arm, {"pick": real})["result"] == "real"
    assert ve.record(pairs[1][0], pairs[1][1], {"pick": "unsure"})["result"] == "unsure"
    with pytest.raises(ValueError):
        ve.record(item, arm, {"pick": "both"})
    item2, arm2 = pairs[2]
    wrong = "2" if ve.sides(item2, arm2) == "1" else "1"
    assert ve.record(item2, arm2, {"pick": wrong})["result"] == "generated"
    assert other in ("1", "2")
    s = ve.summary()
    assert sum(a["votes"] for a in s["arms"].values()) == 3
    assert s["items"] == 4 and s["real"]["median_words"] > 0


def test_the_page_answers_only_the_owner_and_shows_no_arm_before_the_vote(env):
    chat = Chat()
    ve.build(2, "2024-01-01", "2024-02-01", con=env, chat=chat)
    ve.generate(list(ve.ARMS), chat=chat, con=env, embedder=emb)
    pairs = ve.assignment(rl.read(ve.ITEMS), ve.bodies())
    srv = ve.make_server(0, pairs=pairs, tok="tok123", owner_login="kit@example.edu")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/voice-eval-tok123"

    def get(path, login="kit@example.edu", data=None):
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Tailscale-User-Login": login} if login else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""
    try:
        assert get("/", login="")[0] == 403
        assert get("/", login="someone@example.edu")[0] == 403
        code, page = get("/")
        assert code == 200 and "Which one is yours?" in page
        code, page = get("/item/1")
        assert code == 200 and "Email 1 is mine" in page and "Arm:" not in page
        code, page = get("/item/1", data=b"pick=unsure")
        assert code == 200 and "You could not tell." in page and "Arm:" in page
        assert get("/item/9")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_numbers_only_keeps_the_rates_and_drops_the_samples():
    p = json.dumps({"found": True, "median_words": 44, "recent_examples": [{"snippet": "x"}],
                    "closing": "Thanks, this is a whole sentence"})
    import email_writer
    out = json.loads(email_writer.numbers_only(p))
    assert out == {"found": True, "median_words": 44}
    assert email_writer.numbers_only("not json") == "not json"


def test_the_warm_and_hot_arms_sample_and_the_others_do_not(env):
    import email_writer
    chat = Chat()
    seen = []
    real = chat.__call__

    def spy(messages, **kw):
        seen.append((kw.get("purpose"), kw.get("temperature"), (kw.get("sampling") or {}).get("top_k")))
        return real(messages, **kw)
    ve.build(1, "2024-01-01", "2024-02-01", con=env, chat=chat)
    ve.generate(["examples_only", "examples_warm", "examples_hot"], chat=spy, con=env, embedder=emb)
    temps = sorted((t, k) for p, t, k in seen if p == "voice-eval" and t is not None)
    assert temps == [(email_writer.SAMPLING["warm"]["temperature"], 20),
                     (email_writer.SAMPLING["hot"]["temperature"], 40)]
    assert sum(1 for p, t, _ in seen if p == "voice-eval" and t is None) == 1
