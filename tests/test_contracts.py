from app.contracts import Event, RunRequest


def test_run_identifies_workflow_and_revision():
    event = Event("external-1", "comment", "conversation-1", {"text": "pricing"})
    run = RunRequest("run-1", "workflow-1", "project-1", "commit-1", None, event, 1)
    assert run.workflow_id == "workflow-1"
    assert run.commit == "commit-1"
    assert run.test_session_id is None
