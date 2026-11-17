from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from app.coding_agent import CodingAgent


class Backend:
    def __init__(self):
        self.calls = []

    def run(self, message, *, cwd, session_id, test_api_url):
        self.calls.append((message, session_id, test_api_url))
        time.sleep(0.01)
        (cwd / "automation.py").write_text(message)
        return SimpleNamespace(session_id=session_id or "session-" + message, response_text="Done " + message)


def test_sessions_history_and_commits_survive_recreation(tmp_path):
    backend = Backend()
    def checkout(project, dest):
        dest.mkdir()
    def save(project, dest, message):
        return (dest / "automation.py").read_text()
    agent = CodingAgent(tmp_path, checkout, save, backend=backend)
    assert agent.chat("one", "first", "http://test") == {"reply": "Done first", "commit": "first", "session_id": "session-first"}
    agent.chat("two", "other")
    agent = CodingAgent(tmp_path, checkout, save, backend=backend)
    agent.chat("one", "second")
    assert backend.calls == [("first", None, "http://test"), ("other", None, None), ("second", "session-first", None)]
    assert agent.history("one") == [{"role": "user", "text": "first"}, {"role": "assistant", "text": "Done first"}, {"role": "user", "text": "second"}, {"role": "assistant", "text": "Done second"}]


def test_same_project_is_serialized(tmp_path):
    backend = Backend()
    agent = CodingAgent(tmp_path, lambda p, d: d.mkdir(), lambda p, d, m: (d / "automation.py").read_text(), backend=backend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda m: agent.chat("one", m), ["first", "second"]))
    assert backend.calls[1][1] == "session-" + backend.calls[0][0]


def test_unconfigured_backend_fails_closed(tmp_path):
    agent = CodingAgent(tmp_path, lambda *a: None, lambda *a: None)
    with pytest.raises(RuntimeError, match="isolated"):
        agent.chat("one", "hello")


def test_failed_turn_edits_are_discarded_and_checkout_refreshed(tmp_path):
    class FailingBackend(Backend):
        def run(self, message, **kwargs):
            if message == "fail":
                (kwargs["cwd"] / "failed").write_text("partial")
                raise RuntimeError("failed")
            assert not (kwargs["cwd"] / "failed").exists()
            assert (kwargs["cwd"] / "current").read_text() == "updated"
            return super().run(message, **kwargs)
    source = {"value": "initial"}
    def checkout(project, dest):
        dest.mkdir()
        (dest / "current").write_text(source["value"])
    agent = CodingAgent(tmp_path, checkout, lambda p, d, m: "commit", backend=FailingBackend())
    with pytest.raises(RuntimeError, match="failed"):
        agent.chat("one", "fail")
    source["value"] = "updated"
    agent.chat("one", "success")
    assert len(agent.history("one")) == 2


def test_codex_adapter_passes_session_and_scope(monkeypatch, tmp_path):
    import sys
    from app.coding_agent import CodexBackend
    class Options:
        def __init__(self, **kwargs):
            self.values = kwargs
    monkeypatch.setitem(sys.modules, "coloph_agent_runtime", SimpleNamespace(CodexRunOptions=Options))
    calls = []
    class Client:
        def run(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "result"
    backend = CodexBackend(Client())
    assert backend.run("hello", cwd=tmp_path, session_id="thread", test_api_url="http://test") == "result"
    prompt, args = calls[0]
    assert "nontechnical" in prompt and "main" in prompt
    assert args["session_id"] == "thread" and args["resume"] is True
    assert args["env_overrides"] == {"SELF_CODING_TEST_API_URL": "http://test"}
    assert args["options"].values["sandbox"] == "workspace-write"


def test_resume_keeps_explicit_cli_permissions():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("codex_entry", Path("agent-image/codex_entry.py"))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    normalized = shim.normalize([
        "exec", "--json", "-c", 'sandbox_mode="danger-full-access"',
        "-c", 'approval_policy="never"', "resume", "session", "--output-schema", "schema",
    ])
    assert normalized == [
        "exec", "--json", "resume", "-c", 'sandbox_mode="danger-full-access"',
        "-c", 'approval_policy="never"', "session", "--output-schema", "schema",
    ]


def test_backend_progress_survives_refresh_and_failed_turn(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()

    class WaitingBackend(Backend):
        def run(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().run(*args, **kwargs)

    checkout = lambda project, dest: dest.mkdir()
    save = lambda project, dest, message: "commit"
    agent = CodingAgent(tmp_path, checkout, save, backend=WaitingBackend())
    thread = threading.Thread(target=lambda: agent.chat("project", "build"))
    thread.start()
    assert entered.wait(5)
    other_view = CodingAgent(tmp_path, checkout, save, backend=Backend())
    assert other_view.progress("project")["active"][0]["status"] == "running"
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert other_view.progress("project")["active"] == []
    assert other_view.progress("project")["latest"]["status"] == "completed"

    class BrokenBackend:
        def run(self, *args, **kwargs):
            raise RuntimeError("backend stopped")

    agent.backend = BrokenBackend()
    with pytest.raises(RuntimeError):
        agent.chat("project", "change")
    assert other_view.progress("project")["latest"]["status"] == "failed"
    agent.close()
    other_view.close()


def test_stopped_owner_turn_is_recovered_as_interrupted(tmp_path):
    import sqlite3
    agent = CodingAgent(tmp_path, None, None)
    with sqlite3.connect(agent.db) as db:
        db.execute("INSERT INTO chat_turns VALUES ('turn','project',?,'running','build',0,NULL)",
                   (agent.owner,))
    agent.close()
    restarted = CodingAgent(tmp_path, None, None)
    progress = restarted.progress("project")
    assert progress["active"] == []
    assert progress["latest"]["status"] == "interrupted"
    restarted.close()
