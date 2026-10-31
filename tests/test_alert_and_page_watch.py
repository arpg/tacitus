"""alert.py and page_watch.py: the loud channel for a moment with a clock time."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import alert  # noqa: E402
import page_watch as pw  # noqa: E402
import resident_io as rio  # noqa: E402


@pytest.fixture(autouse=True)
def direct_resident_io(monkeypatch):
    """Scratch paths, written directly: no resident child in tests."""
    monkeypatch.setattr(rio, "_owner_check", lambda: False)


@pytest.fixture(autouse=True)
def web_pushes(monkeypatch):
    """The web chat pushes alert.py asks for, recorded: a test never reads
    the owner's chat block or reaches a push service."""
    import chat.direct as direct
    calls: list = []
    monkeypatch.setattr(direct, "web_notify",
                        lambda text, **kw: calls.append(("notify", text, kw)) or {"queued": True})
    monkeypatch.setattr(direct, "web_cancel",
                        lambda tag, **kw: calls.append(("cancel", tag, kw)) or True)
    return calls


def test_parse_at_defaults_to_mountain_time():
    dt = alert.parse_at("2026-10-06T10:00")
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=-6)
    assert alert.parse_at("2026-10-06T10:00-04:00").utcoffset() == timedelta(hours=-4)


def test_default_text_names_the_moment_and_the_row():
    row = {"description": "Buy 2 tickets for the science show. On sale Tue Oct 6 at 10:00am MT.",
           "notes": "Source: https://www.example.edu/today/2026/09/23/science-show ."}
    text = alert.default_text(row, "show-tickets-nov4", alert.parse_at("2026-10-06T10:00"), 15)
    assert text.startswith(":rotating_light: In 15 minutes, at 10:00 am MT: Buy 2 tickets")
    assert "https://www.example.edu/today/2026/09/23/science-show" in text
    assert text.endswith("Ledger `show-tickets-nov4`.")


def test_schedule_without_a_credential_stages_for_flush(tmp_path, monkeypatch):
    """A lane that cannot reach the owner's chat credential (the resident
    runs as ea-bot) must not lose the alert: it is staged, and the
    owner's cron flushes it."""
    monkeypatch.setattr(alert, "PENDING", tmp_path / "pending-alerts.jsonl")
    monkeypatch.setattr(alert, "_row", lambda lid: {"id": lid, "description": "Tickets at 10:00am"})
    import chat
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: (_ for _ in ()).throw(KeyError("ZULIP_API_KEY")))
    at = datetime.now(timezone.utc) + timedelta(days=2)
    out = alert.schedule("tix", at, 15, None)
    assert out.get("staged") is True, out
    lines = (tmp_path / "pending-alerts.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "tix"
    # a moment already past is refused, not staged
    out = alert.schedule("tix", datetime.now(timezone.utc) - timedelta(minutes=1), 15, None)
    assert "past" in out.get("error", "")


def test_schedule_records_on_the_row_and_is_idempotent(monkeypatch, web_pushes):
    calls = []
    monkeypatch.setattr(alert, "_ledger", lambda args: calls.append(args) or {})
    row = {"id": "tix", "description": "Tickets at 10:00am"}
    monkeypatch.setattr(alert, "_row", lambda lid: row)

    class FE:
        owner_id = "8"
    import chat.direct as direct
    monkeypatch.setattr(direct, "frontend", lambda: FE())
    monkeypatch.setattr(direct, "schedule_direct", lambda text, when, fe: 77)
    at = datetime.now(timezone.utc) + timedelta(hours=3)
    out = alert.schedule("tix", at, 15, None)
    assert out["ok"] and out["scheduled_id"] == 77 and out["kind"] == "scheduled"
    # the web chat gets the same text, queued for the moment the alert fires
    assert web_pushes == [("notify", out["text"], {"tag": "alert:tix",
                                                   "when": at - timedelta(minutes=15)})]
    assert out["web"] == {"queued": True}
    assert calls and calls[0][:3] == ["update", "--id", "tix"]
    assert any(a.startswith("alert_scheduled_id=77") for a in calls[0])
    # the row now carries the alert (ledger.py merges metadata keys into the row): a second schedule does nothing
    row.update({"alert_scheduled_id": "77", "alert_at": at.isoformat()})
    out2 = alert.schedule("tix", at, 15, None)
    assert out2.get("already") is True
    # lead already passed but the moment is ahead: send now instead
    sent = []
    monkeypatch.setattr(direct, "send_direct", lambda text, fe: sent.append(text) or {"id": 5})
    row["alert_scheduled_id"] = ""
    out3 = alert.schedule("tix", datetime.now(timezone.utc) + timedelta(minutes=5), 15, None)
    assert out3["ok"] and out3["kind"].startswith("sent now") and sent
    # sent now: the push goes now, and one queued for the row before is dropped
    assert web_pushes[-2:] == [("cancel", "alert:tix", {}),
                               ("notify", sent[0], {"tag": "alert:tix"})]
    assert sent[0].endswith("Ledger `tix`."), sent[0]
    # a custom text gets the ledger tag too, so `list` can name the row
    out4 = alert.schedule("tix", datetime.now(timezone.utc) + timedelta(minutes=5), 15, "custom words")
    assert sent[-1] == "custom words\nLedger `tix`."


def test_page_watch_condition_and_single_fire(tmp_path, monkeypatch):
    """`absent` fires when the sentence disappears, once per flip; a thin
    page (fetch failure) never fires; `present` fires on appearance."""
    monkeypatch.setattr(pw, "WATCHES", tmp_path / "watches.json")
    monkeypatch.setattr(pw, "STATE", tmp_path / "state.json")
    sold = "x" * 300 + " Tickets are sold out, and the waitlist is completely full. " + "y" * 300
    opened = "x" * 300 + " A limited number of tickets are available. " + "y" * 300
    pages = {"u": sold}
    monkeypatch.setattr(pw, "fetch", lambda url: pages[url])
    notified = []
    monkeypatch.setattr(pw, "notify", lambda w, why: notified.append((w["id"], why)) or {"direct": {"id": 1}})
    pw._save(pw.WATCHES, [{"id": "show-tickets", "url": "u", "absent": "Tickets are sold out", "present": "",
                           "message": "Show page changed", "ledger_id": "", "every": 15}])
    r1 = pw.check(force=True)
    assert r1["watches"][0]["fired"] is False and notified == []
    pages["u"] = opened
    r2 = pw.check(force=True)
    assert r2["watches"][0]["fired"] is True and notified == [("show-tickets", "pattern gone")]
    # still open on the next check: no second notification
    pw.check(force=True)
    assert len(notified) == 1
    # page goes back to sold out, then opens again: fires again
    pages["u"] = sold
    pw.check(force=True)
    pages["u"] = opened
    pw.check(force=True)
    assert len(notified) == 2
    # a failed or empty fetch is not a change
    pages["u"] = ""
    r = pw.check(force=True)
    assert r["watches"][0]["fired"] is False and "too short" in r["watches"][0]["why"]
    # the interval is honoured without --force
    r = pw.check()
    assert "skipped" in r["watches"][0]
    assert pw.condition_holds(opened, {"present": "tickets are available"}) == (True, "pattern appeared")


# ── the web chat alone ("frontend": "web") ────────────────────────────

@pytest.fixture
def web_only(tmp_path, monkeypatch):
    """A web-only chat block in the owner-private chat.json, read by an
    owner process; the bot serves the store beside it, and the ledger is a
    dict that `update --set` changes."""
    import chat
    import ea_config
    cfg = tmp_path / "ea-config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(alert, "PENDING", tmp_path / "pending-alerts.jsonl")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "chat.json").write_text(json.dumps(
        {"frontend": "web", "web": {"port": 0, "tailscale_login": "owner@example.test"}}))
    (private / "chat.json").chmod(0o600)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(private))
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    rows: dict = {}

    def ledger(args):
        if args[:1] == ["update"]:
            for kv in args[args.index("--set") + 1:]:
                key, _, value = kv.partition("=")
                rows[args[2]][key] = value
        return {}
    monkeypatch.setattr(alert, "_ledger", ledger)
    monkeypatch.setattr(alert, "_row", lambda lid: dict(rows.get(lid, {})))
    bot = chat.load_frontend(env={})
    assert bot.name == "web" and not bot.writer_only
    bot.tail_s = 0.05
    pushed: list = []
    bot.notify = lambda *a, **kw: pushed.append(a) or {}
    bot.start()
    from chat.web_frontend import WebStore
    yield SimpleNamespace(bot=bot, rows=rows, store=WebStore(private / "webchat.sqlite"),
                          chat=chat, pushed=pushed)
    bot.stop()


def _until(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_web_only_alert_waits_in_the_store_and_the_bot_posts_it_in_the_dm(web_only,
                                                                            web_pushes):
    web_only.rows["tix"] = {"id": "tix", "description": "Tickets at 10:00am"}
    at = datetime.now(timezone.utc) + timedelta(hours=3)
    out = alert.schedule("tix", at, 15, None)
    assert out["ok"] and out["kind"] == "scheduled", out
    (queued,) = web_only.store.posts_queued()
    assert (queued["id"], queued["channel"], queued["text"]) == (out["scheduled_id"], "dm",
                                                                  out["text"])
    assert queued["due_at"] == pytest.approx((at - timedelta(minutes=15)).timestamp())
    assert web_only.rows["tix"]["alert_scheduled_id"] == str(queued["id"])
    # the push is queued for the same moment, as on Zulip
    assert web_pushes == [("notify", out["text"], {"tag": "alert:tix",
                                                   "when": at - timedelta(minutes=15)})]
    # `list` names the row from the queued post
    (listed,) = alert.list_alerts()["scheduled"]
    assert (listed["scheduled_id"], listed["ledger_id"]) == (queued["id"], "tix")
    # not due: the bot's tail thread posts nothing
    time.sleep(0.3)
    assert web_only.store.threads("dm") == [] and len(web_only.store.posts_queued()) == 1
    # due (the clock moved on): the bot posts it in the DM, once, as itself
    with web_only.store._lock, web_only.store._conn:
        web_only.store._conn.execute("UPDATE scheduled_posts SET due_at=?", (time.time() - 1,))
    assert _until(lambda: web_only.store.threads("dm"))
    (posted,) = web_only.store.threads("dm")
    assert posted["author"] == "bot" and posted["text"] == web_only.bot._out(out["text"])
    assert posted["text"].endswith("Ledger `tix`.")
    assert web_only.store.posts_queued() == [] and web_only.bot.post_due() == 0
    assert web_only.store.threads("main") == [] and web_only.pushed == []
    # posted already: cancel finds nothing, as Zulip's would
    from chat import direct
    assert direct.cancel_scheduled(queued["id"]) is False


def test_cancel_drops_a_web_only_alert_before_it_posts(web_only, web_pushes):
    web_only.rows["tix"] = {"id": "tix", "description": "Tickets at 10:00am"}
    at = datetime.now(timezone.utc) + timedelta(hours=3)
    first = alert.schedule("tix", at, 15, None)
    # --force replaces it: the first queued post goes, the new one stays
    second = alert.schedule("tix", at, 30, None, force=True)
    assert [q["id"] for q in web_only.store.posts_queued()] == [second["scheduled_id"]]
    assert second["scheduled_id"] != first["scheduled_id"]
    out = alert.cancel("tix")
    assert out == {"ok": True, "cancelled": str(second["scheduled_id"])}
    assert web_only.store.posts_queued() == [] and alert.list_alerts()["scheduled"] == []
    assert web_pushes[-1] == ("cancel", "alert:tix", {})
    assert web_only.bot.post_due(now=time.time() + 86400) == 0
    assert web_only.store.threads("dm") == []


def test_a_lane_that_is_not_the_owners_stages_a_web_only_alert(tmp_path, monkeypatch):
    """The web chat's store is the owner's: a lane that is not the owner's
    never writes one the bot does not read. Its alert is staged for the
    owner's flush, as on Zulip without the credential."""
    import chat
    from chat import direct
    from chat.web_frontend import WebFrontend
    other = tmp_path / "state" / "webchat.sqlite"
    monkeypatch.setattr(direct, "load_frontend", lambda **kw: WebFrontend.from_config(
        {}, store_path=other, writer_only=True))
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: False)
    monkeypatch.setattr(alert, "PENDING", tmp_path / "pending-alerts.jsonl")
    monkeypatch.setattr(alert, "_row", lambda lid: {"id": lid, "description": "Tickets"})
    out = alert.schedule("tix", datetime.now(timezone.utc) + timedelta(hours=3), 15, None)
    assert out.get("staged") is True and "PermissionError" in out["reason"], out
    with pytest.raises(PermissionError):
        direct.schedule_direct("x", datetime.now(timezone.utc) + timedelta(hours=1),
                               WebFrontend.from_config({}, store_path=other, writer_only=True))
    assert not other.exists()


def test_the_zulip_path_is_unchanged(monkeypatch):
    """The scheduled direct message on Zulip: the same requests as before
    the web chat could hold one, and no owner check of the web's."""
    import chat
    from chat import direct
    from chat.memory_frontend import MemoryFrontend
    calls = []

    class Zulip:
        name = "zulip"
        owner_id = "8"
        client = object()

        def _call(self, *args):
            calls.append(args)
            if args[1:2] == ("POST",):
                return {"result": "success", "scheduled_message_id": 42}
            if args[1:2] == ("DELETE",):
                return {"result": "success"}
            return {"result": "success", "scheduled_messages": [
                {"scheduled_message_id": 42, "type": "private", "content": "hi",
                 "scheduled_delivery_timestamp": 1793421333},
                {"scheduled_message_id": 43, "type": "stream", "content": "not direct",
                 "scheduled_delivery_timestamp": 1793421333}]}

    monkeypatch.setattr(chat.owner_trust, "is_owner_process",
                        lambda: pytest.fail("the Zulip path asks for no owner check"))
    monkeypatch.setattr(direct, "load_frontend", lambda **kw: Zulip())
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    assert direct.schedule_direct("hi", when) == 42
    assert direct.list_scheduled() == [
        {"id": 42, "at": datetime.fromtimestamp(1793421333, timezone.utc), "content": "hi"}]
    assert direct.cancel_scheduled(42) is True
    assert calls == [
        ("scheduled_messages", "POST", {"type": "direct", "to": "[8]", "content": "hi",
                                        "scheduled_delivery_timestamp": int(when.timestamp())}),
        ("scheduled_messages",),
        ("scheduled_messages/42", "DELETE")]
    with pytest.raises(ValueError):
        direct.schedule_direct("hi", when.replace(tzinfo=None), Zulip())
    # A front end with no schedule of its own (Slack, the in-memory one) still raises.
    other = MemoryFrontend()
    with pytest.raises(RuntimeError):
        direct.schedule_direct("hi", when, other)
    assert direct.list_scheduled(other) == [] and direct.cancel_scheduled(1, other) is False
