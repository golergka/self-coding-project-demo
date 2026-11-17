import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.contracts import Event
from app.test_environment import TestEnvironment as Environment


@pytest.fixture
def env(tmp_path):
    calls = []
    environment = Environment(tmp_path / "test.db", lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "run"})
    environment.PROMPT_TIMEOUT_SECONDS = 0.3
    environment.POLL_SECONDS = 0.005
    return environment, calls


def wait_for_prompt(environment, session_id):
    limit = time.monotonic() + 2
    while time.monotonic() < limit:
        timeline = environment.get(session_id)["timeline"]
        if timeline:
            return timeline[-1]
        time.sleep(0.005)
    pytest.fail("prompt did not appear")


def test_session_pins_commit_and_injects_events_into_queue(env):
    environment, calls = env
    before = time.time()
    session = environment.start("project", "intermediate-commit")
    assert before < session["expires_at"] <= time.time() + 3600
    event = Event("event", "comment", "conversation", {"text": "pricing"})
    assert environment.inject(session["id"], event) == {"id": "run"}
    assert calls == [(('project', event), {'test_session_id': session['id'], 'commit': 'intermediate-commit'})]
    dm = Event("dm", "dm", "conversation", {"text": "hello"})
    environment.inject(session["id"], dm)
    assert calls[-1][0][1] == dm
    with pytest.raises(ValueError):
        environment.inject(session["id"], Event("x", "schedule", "c", {}))


def test_fixtures_and_timelines_are_session_scoped_and_persisted(env, tmp_path):
    environment, _ = env
    first = environment.start("p", "a", {"social.dm": {"ok": True}})
    second = environment.start("p", "b", {"social.dm": {"ok": False}})
    assert environment.call(first["id"], "social.dm", {"text": "hello"}) == {"ok": True}
    assert environment.call(second["id"], "social.dm", {}) == {"ok": False}
    restored = Environment(tmp_path / "test.db", lambda *a, **k: None)
    entry = restored.get(first["id"])["timeline"][0]
    assert (entry["operation"], entry["request"], entry["response"], entry["status"]) == (
        "social.dm", {"text": "hello"}, {"ok": True}, "answered")
    assert len(restored.get(second["id"])["timeline"]) == 1


def test_prompt_can_be_answered_only_once_and_only_in_its_session(env):
    environment, _ = env
    session = environment.start("p", "c")
    other = environment.start("p", "c")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(environment.call, session["id"], "social.reply", {"text": "hi"})
        prompt = wait_for_prompt(environment, session["id"])
        assert prompt["status"] == "pending"
        with pytest.raises(KeyError):
            environment.answer(other["id"], prompt["id"], {"ok": True})
        environment.answer(session["id"], prompt["id"], {"ok": True})
        assert future.result(timeout=2) == {"ok": True}
    with pytest.raises(ValueError):
        environment.answer(session["id"], prompt["id"], {"ok": False})


def test_timeout_and_session_expiry_resolve_pending_requests(env):
    environment, _ = env
    session = environment.start("p", "c")
    assert environment.call(session["id"], "social.dm", {}) == {"ok": False, "error": "prompt expired"}
    assert environment.get(session["id"])["timeline"][0]["status"] == "expired"
    environment.SESSION_SECONDS = 0.08
    environment.PROMPT_TIMEOUT_SECONDS = 1
    expiring = environment.start("p", "c")
    assert environment.call(expiring["id"], "social.dm", {}) == {"ok": False, "error": "prompt expired"}
    with pytest.raises(ValueError):
        environment.inject(expiring["id"], Event("x", "dm", "c", {}))
    assert environment.call(expiring["id"], "social.dm", {}) == {"ok": False, "error": "test session expired"}


def test_concurrent_answers_have_one_winner(env):
    environment, _ = env
    session = environment.start("p", "c")
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = pool.submit(environment.call, session["id"], "social.dm", {})
        prompt = wait_for_prompt(environment, session["id"])
        barrier = threading.Barrier(2)
        def answer(value):
            barrier.wait()
            try:
                environment.answer(session["id"], prompt["id"], {"value": value})
                return value
            except ValueError:
                return None
        attempts = [pool.submit(answer, value) for value in (1, 2)]
        winners = [future.result(timeout=2) for future in attempts]
        assert len([winner for winner in winners if winner is not None]) == 1
        assert pending.result(timeout=2) == {"value": next(winner for winner in winners if winner is not None)}


def test_invalid_and_missing_requests(env):
    environment, _ = env
    with pytest.raises(ValueError):
        environment.start("p", "c", {"social.dm": None})
    with pytest.raises(KeyError):
        environment.get("missing")
    session = environment.start("p", "c")
    with pytest.raises(ValueError):
        environment.call(session["id"], "social.dm", {"bad": object()})
    with pytest.raises(KeyError):
        environment.answer(session["id"], "missing", {})
