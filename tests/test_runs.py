import sqlite3
import threading
import time

import pytest

from app.contracts import Event, RunResult
from app.runs import Runs


def event(id, kind="comment", conversation="person"):
    return Event(id, kind, conversation, {"text": id})


def test_pinning_dedup_and_restart(tmp_path):
    project = {"main_commit": "first"}
    calls = []
    db = tmp_path / "runs.db"
    runs = Runs(db, lambda _: project, lambda run: calls.append(run) or RunResult("waiting"))
    first = runs.enqueue("p", event("1"))
    project["main_commit"] = "second"
    reply = runs.enqueue("p", event("2", "dm"))
    assert reply["workflow_id"] == first["workflow_id"]
    assert reply["commit"] == "first"
    assert runs.enqueue("p", event("1"))["id"] == first["id"]
    runs = Runs(db, lambda _: project, lambda run: calls.append(run) or RunResult("waiting"))
    assert runs.work_once() and runs.work_once() and not runs.work_once()
    assert [r.commit for r in calls] == ["first", "first"]
    assert [r.status if hasattr(r, "status") else r.attempt for r in calls] == [1, 1]


def test_schedule_dedup_and_test_scope(tmp_path):
    calls = []
    scheduled = []
    def execute(request):
        calls.append(request)
        if request.event.kind != "schedule":
            a = runs.schedule(request, 0, {"x": 1}, "followup")
            assert runs.schedule(request, 0, {"x": 2}, "followup")["id"] == a["id"]
            scheduled.append(a)
        return RunResult("waiting")
    runs = Runs(tmp_path / "db", lambda _: {"main_commit": "live"}, execute)
    with pytest.raises(ValueError):
        runs.enqueue("p", event("bad"), test_session_id="s")
    runs.enqueue("p", event("1"), test_session_id="s", commit="branch-commit")
    assert runs.work_once()
    assert len(scheduled) == 1
    with pytest.raises(ValueError, match="lease"):
        runs.schedule(calls[0], 0, {}, "stale")
    assert runs.work_once()
    assert calls[-1].commit == "branch-commit" and calls[-1].event.kind == "schedule"
    assert calls[-1].event.payload == {"x": 1}
    assert runs.enqueue("p", event("orphan", "dm"))["status"] == "completed"


def test_retry_and_recovery(tmp_path):
    attempts = []
    def execute(run):
        attempts.append(run.attempt)
        return RunResult("failed", ["log"], "try again", True) if run.attempt == 1 else RunResult("completed")
    db = tmp_path / "db"
    runs = Runs(db, lambda _: {"main_commit": "x"}, execute)
    record = runs.enqueue("p", event("1"))
    assert runs.work_once()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE runs SET status='running', lease_until=0 WHERE id=?", (record["id"],))
        c.execute("UPDATE runs SET due=0 WHERE id=?", (record["id"],))
    assert runs.work_once()
    assert attempts == [1, 2]
    assert runs.list("p")[0]["status"] == "completed"
    assert runs.list("p")[0]["logs"] == ["log"]


def test_serializes_conversation_and_allows_other_conversations(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(run):
        calls.append(run.event.id)
        if run.event.id == "1":
            entered.set()
            assert release.wait(5)
        return RunResult("waiting")
    runs = Runs(tmp_path / "db", lambda _: {"main_commit": "x"}, execute)
    runs.enqueue("p", event("1"))
    runs.enqueue("p", event("2", "dm"))
    runs.enqueue("p", event("3", conversation="other"))
    t = threading.Thread(target=runs.work_once)
    t.start()
    assert entered.wait(5)
    assert runs.work_once()
    assert calls == ["1", "3"]
    assert not runs.work_once()
    release.set()
    t.join()
    assert runs.work_once() and calls == ["1", "3", "2"]


def test_subscriptions_and_delayed_schedule_does_not_block_reply(tmp_path):
    calls = []
    def execute(request):
        calls.append(request)
        if request.event.kind == "comment":
            runs.schedule(request, 1000, {}, "later")
        return RunResult("waiting")
    runs = Runs(tmp_path / "db", lambda _: {"main_commit": "x"}, execute)
    runs.sync_subscriptions("p", ["comment", "dm", "comment"])
    assert runs.subscribed("p", "comment")
    runs.sync_subscriptions("p", ["dm"])
    assert not runs.subscribed("p", "comment")
    runs.enqueue("p", event("1"))
    runs.work_once()
    runs.enqueue("p", event("2", "dm"))
    assert runs.work_once() and calls[-1].event.id == "2"


def test_reserved_reply_can_keep_workflow_waiting_after_first_completes(tmp_path):
    results = iter([RunResult("completed"), RunResult("waiting"), RunResult("completed")])
    runs = Runs(tmp_path / "db", lambda _: {"main_commit": "x"}, lambda _: next(results))
    first = runs.enqueue("p", event("1"))
    runs.enqueue("p", event("2", "dm"))
    assert runs.work_once() and runs.work_once()
    reply = runs.enqueue("p", event("3", "dm"))
    assert reply["workflow_id"] == first["workflow_id"]
    assert runs.work_once()


def test_expired_attempt_cannot_mutate_after_takeover(tmp_path):
    from app.service_api import ServiceAPI

    db = tmp_path / "runs.db"
    entered, release = threading.Event(), threading.Event()
    rejected = []
    live_effects = []
    api = ServiceAPI(tmp_path / "api.db", None,
                     lambda *args: live_effects.append(args) or {"ok": True}, None)

    def execute(request):
        if request.attempt == 1:
            entered.set()
            assert release.wait(5)
            for operation, body in (
                ("state.put", {"value": {"attempt": 1}}),
                ("social.dm", {"recipient_id": "person", "text": "stale", "key": "stale-send"}),
            ):
                with pytest.raises(ValueError, match="lease"):
                    api.call(request, operation, body, fence=runs.mutation)
                rejected.append(operation)
            with pytest.raises(ValueError, match="lease"):
                runs.schedule(request, 0, {}, "stale-schedule")
        else:
            api.call(request, "state.put", {"value": {"attempt": 2}}, fence=runs.mutation)
        return RunResult("completed")

    runs = Runs(db, lambda _: {"main_commit": "commit"}, execute)
    record = runs.enqueue("project", event("event"))
    first = threading.Thread(target=runs.work_once)
    first.start()
    assert entered.wait(5)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE runs SET lease_until=0 WHERE id=?", (record["id"],))
    assert runs.work_once()
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert rejected == ["state.put", "social.dm"]
    assert live_effects == []
    with sqlite3.connect(tmp_path / "api.db") as connection:
        assert connection.execute("SELECT value FROM service_state").fetchone()[0] == '{"attempt": 2}'
    assert runs.list("project")[0]["attempt"] == 2
    assert runs.list("project")[0]["status"] == "completed"


def test_mutation_and_takeover_cannot_interleave(tmp_path):
    db = tmp_path / "runs.db"
    request_holder = []
    runs = Runs(db, lambda _: {"main_commit": "commit"},
                lambda request: request_holder.append(request) or RunResult("waiting"))
    record = runs.enqueue("project", event("event"))
    runs.work_once()
    request = request_holder[0]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE runs SET status='running',lease_until=? WHERE id=?",
                           (time.time() + 30, record["id"]))
    contender_started, contender_done = threading.Event(), threading.Event()

    def contender():
        contender_started.set()
        runs.work_once()
        contender_done.set()

    with runs.mutation(request) as connection:
        # Even an expired lease cannot be reclaimed during a guarded write.
        connection.execute("UPDATE runs SET lease_until=0 WHERE id=?", (record["id"],))
        thread = threading.Thread(target=contender)
        thread.start()
        assert contender_started.wait(5)
        assert not contender_done.wait(0.05)
    thread.join(timeout=5)
    assert contender_done.is_set()
    assert request_holder[-1].attempt == 2
