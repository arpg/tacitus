"""mark-read and mark-unread flip the read flag in place on both providers."""
import types

import gmail_client
import graph_client


def test_outlook_mark_read_patches_is_read_only(monkeypatch):
    calls = []
    monkeypatch.setattr(graph_client, "graph_patch", lambda ep, body: calls.append((ep, body)) or {})
    out = graph_client.mark_message_read("AAMk1=")
    assert calls == [(f"/me/messages/{graph_client._id('AAMk1=')}", {"isRead": True})]
    assert out["marked_read"]


def test_gmail_mark_read_removes_the_unread_label_only(monkeypatch):
    seen = {}

    def modify(userId, id, body):
        seen.update(id=id, body=body)
        return types.SimpleNamespace(execute=lambda: {})
    service = types.SimpleNamespace(users=lambda: types.SimpleNamespace(
        messages=lambda: types.SimpleNamespace(modify=modify)))
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, **kw: service)
    out = gmail_client.mark_read("someone@example.org", "m1")
    assert seen == {"id": "m1", "body": {"removeLabelIds": ["UNREAD"]}}
    assert out["status"] == "marked_read"
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, **kw: None)
    assert "error" in gmail_client.mark_read("someone@example.org", "m1")


def test_outlook_mark_unread_patches_is_read_only(monkeypatch):
    calls = []
    monkeypatch.setattr(graph_client, "graph_patch", lambda ep, body: calls.append((ep, body)) or {})
    out = graph_client.mark_message_unread("AAMk1=")
    assert calls == [(f"/me/messages/{graph_client._id('AAMk1=')}", {"isRead": False})]
    assert out["marked_unread"]


def test_gmail_mark_unread_adds_the_unread_label_only(monkeypatch):
    seen = {}

    def modify(userId, id, body):
        seen.update(id=id, body=body)
        return types.SimpleNamespace(execute=lambda: {})
    service = types.SimpleNamespace(users=lambda: types.SimpleNamespace(
        messages=lambda: types.SimpleNamespace(modify=modify)))
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, **kw: service)
    out = gmail_client.mark_unread("someone@example.org", "m1")
    assert seen == {"id": "m1", "body": {"addLabelIds": ["UNREAD"]}}
    assert out["status"] == "marked_unread"
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, **kw: None)
    assert "error" in gmail_client.mark_unread("someone@example.org", "m1")
