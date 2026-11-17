import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def docker_available():
    if not shutil.which("docker"):
        pytest.skip("Docker is required for the real-container integration test")
    result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if result.returncode:
        pytest.skip("Docker daemon is unavailable")
    image = subprocess.run(["docker", "image", "inspect", "python:3.12-alpine"],
                           capture_output=True, timeout=10)
    if image.returncode:
        pytest.skip("Pull python:3.12-alpine to run the container integration test")


def test_chat_can_test_and_deploy_any_commit_independent_of_branch_tip(tmp_path, monkeypatch, docker_available):
    calls = []
    tested = []
    source = Path("app/examples/instagram_pricing.py").read_text()
    test_args = {
        "events": [
            {"id": "comment", "kind": "comment", "conversation_id": "c",
             "payload": {"text": "pricing", "comment_id": "comment", "sender_id": "customer"}},
            {"id": "email", "kind": "dm", "conversation_id": "c",
             "payload": {"text": "customer@example.com", "sender_id": "customer"}},
        ],
        "expected_statuses": ["waiting", "completed"],
        "expected_effects": [
            {"operation": "social.reply", "request": {"text": "Sent you a DM!"}},
            {"operation": "social.dm"},
            {"operation": "social.dm", "request": {"text": "Here is the pricing link: https://example.com/pricing"}},
        ],
    }

    class Backend:
        def __init__(self, *args):
            pass

        def close(self):
            pass

        def run(self, message, *, cwd, session_id, test_api_url):
            step = len(calls)
            calls.append(message)
            if step == 0:
                assert not (cwd / "automation.py").exists()
                operation, args = "project.checkout", {"branch": "feature/pricing"}
            elif step == 1:
                (cwd / "automation.py").write_text(source)
                operation, args = "test.run", test_args
            elif step == 2:
                result, _ = json.JSONDecoder().raw_decode(message.split(":\n", 1)[1])
                assert result["passed"]
                tested.append(result["commit"])
                (cwd / "automation.py").write_text(source.replace("https://example.com/pricing", "https://example.com/newer"))
                operation, args = "test.run", {**test_args, "commit": tested[0]}
            elif step == 3:
                assert '"passed": true' in message
                operation, args = "project.deploy", {"commit": tested[0]}
            elif step == 4:
                assert '"main_commit":' in message
                operation, args = "respond", {}
            else:
                # A later chat resumes its actual Git branch, even if main points elsewhere.
                assert "https://example.com/newer" in (cwd / "automation.py").read_text()
                operation, args = "respond", {}
            return SimpleNamespace(session_id="session", response_text=json.dumps({
                "reply": "Done.", "operation": operation, "arguments_json": json.dumps(args),
            }))

    monkeypatch.setenv("SELF_CODING_CODEX_IMAGE", "test-image")
    monkeypatch.setenv("SELF_CODING_CONTROL_KEY", "browser-test-key")
    monkeypatch.setattr("app.main.CodingWorker", Backend)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/projects").status_code == 403
        client.get("/?key=browser-test-key")
        project = client.post("/api/projects", json={"name": "English project"}).json()
        initial = project["main_commit"]
        response = client.post(f"/api/projects/{project['id']}/chat",
                               json={"message": "Work on a branch and test the earlier commit."})
        assert response.status_code == 200, response.text
        project = client.get(f"/api/projects/{project['id']}").json()
        assert project["main_commit"] == tested[0] != initial
        assert project["branches"]["feature/pricing"] == response.json()["commit"] != tested[0]
        actions = client.get(f"/api/projects/{project['id']}/actions").json()
        assert [action["operation"] for action in actions] == [
            "project.checkout", "test.run", "test.run", "project.deploy",
        ]
        assert actions[1]["result"]["passed"] and actions[2]["result"]["passed"]
        assert actions[2]["commit"] != actions[2]["result"]["commit"] == tested[0]
        assert set(project) == {"id", "user_id", "name", "main_commit", "branches"}
        again = client.post(f"/api/projects/{project['id']}/chat", json={"message": "What is on my branch?"})
        assert again.status_code == 200, again.text
        assert len(calls) == 6


def test_unknown_chat_tool_has_no_side_effect_and_keeps_error_evidence(tmp_path):
    from app.chat_tools import ChatTools

    class Projects:
        def get(self, project):
            return {"id": project}

    tools = ChatTools(tmp_path / "db", Projects(), None, None, None,
                      lambda *args: pytest.fail("Unexpected deployment"))
    result = tools.call("one", "external.deploy", {"project_id": "other"}, "commit")
    assert result["ok"] is False
    assert tools.history("one")[0]["status"] == "failed"
    assert tools.history("other") == []


def test_malformed_arguments_return_recoverable_tool_error(tmp_path):
    from app.chat_tools import ChatTools
    projects = SimpleNamespace(get=lambda project: {"id": project})
    tools = ChatTools(tmp_path / "db", projects, None, None, None, None)
    result = tools.call("project", "test.run", "not an object", "commit")
    assert not result["ok"]
    assert tools.history("project")[0]["arguments"] == "not an object"


def test_deployed_report_uses_pinned_run_commit_and_reports_scheduled_work(tmp_path):
    from app.chat_tools import ChatTools
    completed = {"id": "reply", "workflow_id": "workflow", "commit": "old",
                 "status": "completed"}
    pending = {"id": "followup", "workflow_id": "workflow", "commit": "old",
               "status": "queued"}
    projects = SimpleNamespace(get=lambda project: {"main_commit": "new"})
    runs = SimpleNamespace(
        subscribed=lambda *args: True,
        enqueue=lambda *args: completed,
        list=lambda project: [completed, pending],
    )
    social = SimpleNamespace(history=lambda project: [], ingest=lambda *args: None)
    tools = ChatTools(tmp_path / "db", projects, runs, None, social, None)
    result = tools.call("project", "demo.events", {
        "events": [{"id": "reply", "kind": "dm", "conversation_id": "conversation",
                    "payload": {"text": "customer@example.com"}}],
        "expected_statuses": ["completed"],
    }, "new")
    assert result["passed"]
    assert result["commit"] == "old"
    assert result["commits"] == ["old"]
    assert result["pending_runs"] == [pending]


def test_moving_main_to_an_older_commit_keeps_its_checkout_consistent(tmp_path):
    from app.chat_tools import ChatTools
    from app.projects import Projects
    projects = Projects(tmp_path / "projects")
    project = projects.create("user", "Git project")
    checkout = tmp_path / "checkout"
    projects.checkout(project["id"], checkout)
    (checkout / "automation.py").write_text("first")
    first = projects.save(project["id"], checkout, "First")
    (checkout / "automation.py").write_text("second")
    second = projects.save(project["id"], checkout, "Second")
    tools = ChatTools(tmp_path / "tools.sqlite", projects, None, None, None, projects.move_main)
    result = tools.call(project["id"], "project.deploy", {"commit": first}, second, checkout=checkout)
    assert result["deployment"]["main_commit"] == first
    assert (checkout / "automation.py").read_text() == "first"
    assert projects.save(project["id"], checkout, "Unchanged") == first
