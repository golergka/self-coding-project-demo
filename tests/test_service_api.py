import pytest

from app.contracts import Event, RunRequest
from app.service_api import ServiceAPI


def run(project="p", workflow="w", session=None, attempt=1):
    return RunRequest("r", workflow, project, "original-commit", session, Event("e", "comment", "c", {}), attempt)


def test_state_is_durable_and_scoped(tmp_path):
    db = tmp_path / "data" / "db"
    api = ServiceAPI(db, None, None, None)
    api.call(run(), "state.put", {"value": {"step": 1}})
    api = ServiceAPI(db, None, None, None)
    assert api.call(run(attempt=2), "state.get", {}) == {"value": {"step": 1}}
    for other in (run(project="other"), run(workflow="other"), run(session="live")):
        assert api.call(other, "state.get", {}) == {"value": None}
    with pytest.raises(ValueError):
        api.call(run(), "state.put", {"value": []})


def test_social_routing_deduplication_and_scope(tmp_path):
    calls = []
    def mock(session, operation, body):
        calls.append((session, operation, body))
        return {"ok": True}
    def live(request, operation, body):
        calls.append((request, operation, body))
        return {"ok": True}
    api = ServiceAPI(tmp_path / "db", mock, live, None)
    body = {"recipient_id": "person", "text": "hello", "key": "step"}
    assert api.call(run(session="s"), "social.dm", body) == {"ok": True}
    assert api.call(run(session="s", attempt=2), "social.dm", body) == {"ok": True}
    assert len(calls) == 1 and calls[0][0] == "s" and calls[0][2]["key"] != "step"
    api.call(run(), "social.dm", body)
    assert calls[1][0] == run() and calls[1][2]["key"] != calls[0][2]["key"]
    with pytest.raises(ValueError):
        api.call(run(), "social.dm", {**body, "text": "changed"})


def test_retry_and_ambiguous_send(tmp_path):
    calls = []
    def live(request, operation, body):
        calls.append(body["key"])
        if len(calls) == 1:
            return {"ok": False, "retryable": True}
        raise TimeoutError()
    api = ServiceAPI(tmp_path / "db", None, live, None)
    body = {"comment_id": "c", "text": "hello", "key": "reply"}
    assert api.call(run(), "social.reply", body)["retryable"]
    assert api.call(run(attempt=2), "social.reply", body)["uncertain"]
    assert api.call(run(attempt=3), "social.reply", body)["uncertain"]
    assert len(calls) == 2 and calls[0] == calls[1]


def test_schedule_delegates_and_validates(tmp_path):
    calls = []
    def schedule(*args):
        calls.append(args)
        return {"id": "scheduled"}
    api = ServiceAPI(tmp_path / "db", None, None, schedule)
    request = run(session="s")
    assert api.call(request, "schedule", {"delay_seconds": 5, "payload": {"x": 1}, "key": "later"}) == {"id": "scheduled"}
    assert calls == [(request, 5, {"x": 1}, "later")]
    with pytest.raises(ValueError):
        api.call(request, "schedule", {"delay_seconds": -1, "payload": {}, "key": "x"})


def test_success_is_never_retried_even_if_adapter_marks_retryable(tmp_path):
    calls = []
    def live(request, operation, body):
        calls.append(body)
        return {"ok": True, "retryable": True}
    api = ServiceAPI(tmp_path / "db", None, live, None)
    body = {"comment_id": "c", "text": "hello", "key": "reply"}
    api.call(run(), "social.reply", body)
    api.call(run(attempt=2), "social.reply", body)
    assert len(calls) == 1


def test_broader_read_api_uses_mock_and_validates_before_dispatch(tmp_path):
    calls = []
    api = ServiceAPI(tmp_path / "db",
                     lambda *args: calls.append(args) or {"ok": True, "posts": []},
                     lambda *args: pytest.fail("test read reached live adapter"), None)
    api.call(run(session="test"), "social.posts.list", {"account_id": "a", "limit": 5})
    api.call(run(session="test"), "social.posts.list", {"account_id": "a", "limit": 5})
    assert len(calls) == 2  # Reads can reflect new service data.
    for body in ({"account_id": "a", "limit": True}, {"account_id": "a", "url": "http://host"}):
        with pytest.raises(ValueError):
            api.call(run(session="test"), "social.posts.list", body)
    assert len(calls) == 2


def test_demo_posts_are_persistent_scoped_and_idempotent(tmp_path):
    from app.demo_social import DemoSocial
    demo = DemoSocial(tmp_path / "social.sqlite")
    api = ServiceAPI(tmp_path / "api.sqlite", None, demo, None)
    request = run()
    created = api.call(request, "social.posts.create",
                       {"account_id": "demo-account", "text": "New post", "key": "create"})
    post_id = created["post"]["id"]
    assert api.call(request, "social.posts.create",
                    {"account_id": "demo-account", "text": "New post", "key": "create"}) == created
    api = ServiceAPI(tmp_path / "api.sqlite", None, DemoSocial(tmp_path / "social.sqlite"), None)
    api.call(request, "social.posts.update", {"post_id": post_id, "text": "Changed", "key": "edit"})
    assert api.call(request, "social.posts.get", {"post_id": post_id})["post"]["text"] == "Changed"
    assert not api.call(run(project="other"), "social.posts.get", {"post_id": post_id})["ok"]
    api.call(request, "social.posts.delete", {"post_id": post_id, "key": "delete"})
    assert not api.call(request, "social.posts.get", {"post_id": post_id})["ok"]


def test_demo_history_includes_provider_events_and_replies(tmp_path):
    from app.demo_social import DemoSocial
    demo = DemoSocial(tmp_path / "social.sqlite")
    request = run()
    demo.ingest("p", Event("incoming", "comment", "c",
                          {"comment_id": "comment-1", "post_id": "demo-post", "text": "pricing"}))
    demo(request, "social.reply", {"comment_id": "comment-1", "text": "Hello", "key": "reply"})
    comments = demo(request, "social.comments.list", {"post_id": "demo-post"})["comments"]
    assert [comment["text"] for comment in comments] == ["pricing", "Hello"]
    demo.ingest("p", Event("dm-in", "dm", "c", {"sender_id": "customer", "text": "Hi"}))
    demo(request, "social.dm", {"recipient_id": "customer", "text": "Welcome", "key": "dm"})
    messages = demo(request, "social.messages.list", {"recipient_id": "customer"})["messages"]
    assert [message["direction"] for message in messages] == ["incoming", "outgoing"]


def test_default_mock_write_shapes_match_the_demo_contract(tmp_path):
    from app.api_catalog import demo_fixtures
    from app.demo_social import DemoSocial
    fixtures = demo_fixtures()
    demo = DemoSocial(tmp_path / "social.sqlite")
    bodies = {
        "social.posts.create": {"account_id": "demo-account", "text": "Post", "key": "create"},
        "social.posts.update": {"post_id": "demo-post", "text": "Changed", "key": "update"},
        "social.reply": {"comment_id": "comment", "text": "Hello", "key": "reply"},
        "social.dm": {"recipient_id": "demo-user", "text": "Hello", "key": "dm"},
    }
    for operation, body in bodies.items():
        actual = demo(run(), operation, body)
        for key, value in actual.items():
            assert key in fixtures[operation]
            assert isinstance(fixtures[operation][key], type(value))
            if isinstance(value, dict):
                assert isinstance(fixtures[operation][key]["id"], str)
