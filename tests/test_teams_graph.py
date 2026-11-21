"""Teams 1:1 chats through Graph (delegated Chat.Read): the reader keeps
recent real messages, stops at the first chat older than the window, names
the other member, and says "not available" (None) without consent."""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import graph_client  # noqa: E402

ME = "oid-me"


def _token(oid=ME):
    payload = base64.urlsafe_b64encode(json.dumps({"oid": oid}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def _chat(cid, last, other="Sam Example"):
    return {"id": cid, "chatType": "oneOnOne",
            "lastMessagePreview": {"createdDateTime": last},
            "members": [{"userId": ME, "displayName": "Me"},
                        {"userId": "oid-" + cid, "displayName": other,
                         "email": f"{cid}@example.org"}]}


def _msg(mid, created, text="hi", kind="message", sender="Sam Example", deleted=None):
    return {"id": mid, "messageType": kind, "createdDateTime": created,
            "deletedDateTime": deleted, "body": {"content": f"<p>{text}</p>"},
            "from": {"user": {"displayName": sender}}}


def test_recent_chats_keep_real_messages_in_the_window(monkeypatch):
    calls = []
    pages = {
        "/me/chats": {"value": [_chat("c1", _ago(hours=2)), _chat("c2", _ago(hours=5)),
                                _chat("c3", _ago(days=3)), _chat("c4", _ago(hours=1))]},
        "/chats/c1/messages": {"value": [
            _msg("m1", _ago(hours=2), "see you <b>at 3</b>"),
            _msg("m2", _ago(hours=3), kind="systemEventMessage"),
            _msg("m3", _ago(hours=4), deleted=_ago(hours=1)),
            _msg("m4", _ago(days=2), "old")]},
        "/chats/c2/messages": {"value": [_msg("m5", _ago(days=2))]},
    }

    def get(path, token, params):
        calls.append(path)
        return pages[path]
    monkeypatch.setattr(graph_client, "_chat_get", get)
    chats = graph_client.get_graph_teams_chats(days_back=1, token=_token())
    assert chats == [{
        "chat_id": "c1", "chat_type": "oneOnOne", "display_name": "Sam Example",
        "email": "c1@example.org",
        "messages": [{"id": "m1", "from": "Sam Example", "body": "see you at 3",
                      "body_preview": "see you at 3",
                      "created": pages["/chats/c1/messages"]["value"][0]["createdDateTime"],
                      "message_type": "message"}]}]
    # c2 had nothing recent; the list stops at c3, older than the window,
    # so c4 after it (out of order) is never read.
    assert calls == ["/me/chats", "/chats/c1/messages", "/chats/c2/messages"]


def test_no_chat_consent_is_none_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(graph_client, "_chat_token", lambda: None)
    assert graph_client.get_graph_teams_chats() is None


def test_the_chat_token_is_asked_for_alone(monkeypatch):
    asked = []

    class App:
        def get_accounts(self):
            return [{"username": "u"}]

        def acquire_token_silent(self, scopes, account):
            asked.append(scopes)
            return {"error": "invalid_grant"}
    monkeypatch.setattr(graph_client, "_app", App())
    assert graph_client._chat_token() is None
    assert asked == [["https://graph.microsoft.com/Chat.Read"]]
    assert not set(graph_client.CHAT_READ_SCOPES) & set(graph_client.SCOPES)


def test_get_teams_chats_is_a_safe_read_and_says_unavailable(monkeypatch, capsys):
    import graph_actions
    import mail_mediation
    assert "get-teams-chats" in mail_mediation.SAFE["graph_actions"]
    # The plugin's verb name stays free: a core verb of that name would hide it.
    assert "get-teams-dms" not in mail_mediation.SAFE["graph_actions"]
    monkeypatch.setattr(graph_actions.graph, "get_graph_teams_chats", lambda days_back=1: None)
    graph_actions.cmd_get_teams_chats(type("A", (), {"days": 2})())
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_group_chats_and_whole_messages_are_read_meeting_chats_skipped(monkeypatch):
    long = "word " * 200
    group = {"id": "g1", "chatType": "group", "topic": "",
             "lastMessagePreview": {"createdDateTime": _ago(hours=1)},
             "members": [{"userId": ME, "displayName": "Me"},
                         {"userId": "oid-a", "displayName": "Ana Example"},
                         {"userId": "oid-b", "displayName": "Bo Example"}]}
    meeting = dict(group, id="t1", chatType="meeting")
    pages = {"/me/chats": {"value": [meeting, group]},
             "/chats/g1/messages": {"value": [_msg("m1", _ago(hours=1), long, sender="Ana Example")]}}
    monkeypatch.setattr(graph_client, "_chat_get", lambda path, token, params: pages[path])
    (chat,) = graph_client.get_graph_teams_chats(days_back=1, token=_token())
    assert (chat["chat_type"], chat["display_name"], chat["email"]) == \
        ("group", "Ana Example, Bo Example", "")
    msg = chat["messages"][0]
    assert msg["body"] == long.strip() and len(msg["body_preview"]) == 300

