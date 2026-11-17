import json
import os
from pathlib import Path

import pytest

from app.contracts import Event, RunRequest
from app.runner import Runner


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
if sys.argv[1] == "rm":
    open(os.environ["DOCKER_ARGS"] + ".removed", "w").write(sys.argv[-1])
    sys.exit(0)
with open(os.environ["DOCKER_ARGS"], "w") as f:
    json.dump(sys.argv[1:], f)
mounts = [sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == "--mount"]
paths = [m.split(",")[1][4:] for m in mounts]
bootstrap = open(paths[1]).read().replace('"/workspace"', repr(paths[0]))
sys.exit(subprocess.call([sys.executable, "-c", bootstrap]))
''')
    docker.chmod(0o755)
    args = tmp_path / "args.json"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DOCKER_ARGS", str(args))
    return args


@pytest.fixture
def run():
    return RunRequest("run", "workflow", "project", "old-commit", "test", Event("event", "comment", "conversation", {"text": "hello"}), 1)


def test_snapshot_scope_rpc_and_container_isolation(fake_docker, run):
    snapshots, calls = [], []

    def snapshot(project, commit, dest):
        snapshots.append((project, commit))
        (dest / "automation.py").write_text('''def handle(event, api):
    print("automation log")
    assert event["payload"]["text"] == "hello"
    assert api("state.get", {}) == {"value": {"step": 1}}
    return "waiting"
''')

    def api(request, operation, body):
        calls.append((request, operation, body))
        return {"value": {"step": 1}}

    result = Runner(snapshot, api).execute(run)
    args = json.loads(fake_docker.read_text())
    assert result.status == "waiting"
    assert "automation log" in "".join(result.logs)
    assert snapshots == [("project", "old-commit")]
    assert calls == [(run, "state.get", {})]
    for option in ("--rm", "--network", "--read-only", "--memory", "--cpus", "--pids-limit", "--user", "--cap-drop"):
        assert option in args
    assert args[args.index("--network") + 1] == "none"
    assert all("readonly" in args[i + 1] for i, value in enumerate(args) if value == "--mount")


def test_api_error_and_automation_exception_are_reported(fake_docker, run):
    def snapshot(project, commit, dest):
        (dest / "automation.py").write_text('''def handle(event, api):
    result = api("social.dm", {})
    assert result == {"ok": False, "error": "denied"}
    raise RuntimeError("automation failed")
''')
    result = Runner(snapshot, lambda *_: {"ok": False, "error": "denied"}).execute(run)
    assert result.status == "failed"
    assert "automation failed" in result.error


def test_timeout_releases_container(fake_docker, run, monkeypatch):
    monkeypatch.setattr("app.runner._TIMEOUT", 0.3)
    def snapshot(project, commit, dest):
        (dest / "automation.py").write_text('def handle(event, api):\n    while True: pass\n')
    result = Runner(snapshot, lambda *_: {}).execute(run)
    assert result.status == "failed"
    assert "deadline" in result.error
    assert result.retryable
    assert Path(str(fake_docker) + ".removed").read_text().startswith("self-coding-")


def test_bad_return_and_snapshot_failure(fake_docker, run):
    def snapshot(project, commit, dest):
        (dest / "automation.py").write_text('def handle(event, api): return "other"\n')
    assert Runner(snapshot, lambda *_: {}).execute(run).status == "failed"
    def fail(*_):
        raise ValueError("missing commit")
    assert Runner(fail, lambda *_: {}).execute(run).error == "missing commit"


def test_oversized_container_frame_is_rejected(fake_docker, run):
    def snapshot(project, commit, dest):
        (dest / "automation.py").write_text("import sys\ndef handle(event, api):\n    sys.__stdout__.write('x' * 70000 + '\\n')\n    sys.__stdout__.flush()\n    return 'completed'\n")
    result = Runner(snapshot, lambda *_: {}).execute(run)
    assert result.status == "failed"
    assert "invalid container frame" in result.error
    assert Path(str(fake_docker) + ".removed").exists()


def test_api_timeout_is_uncertain(fake_docker, run, monkeypatch):
    import time
    monkeypatch.setattr("app.runner._TIMEOUT", 1.0)
    def snapshot(project, commit, dest):
        (dest / "automation.py").write_text("def handle(event, api):\n    api('social.dm', {})\n    return 'completed'\n")
    def slow_api(*_):
        time.sleep(3)
        return {"ok": True}
    result = Runner(snapshot, slow_api).execute(run)
    assert result.status == "uncertain"
    assert not result.retryable
    assert Path(str(fake_docker) + ".removed").exists()
